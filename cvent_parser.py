import html
import json
import re
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import Dict, Any, Optional

def extract_schema_json(html: str) -> Optional[Dict[str, Any]]:
    """
    Finds and parses the Schema.org Event object embedded in Cvent HTML.
    Extracts the balanced JSON block around schema.org/Event and unescapes all escaping layers.
    """
    idx = html.find('https://schema.org')
    if idx == -1:
        idx = html.find('schema.org')
    if idx == -1:
        return None

    # Search backward to find the opening '{' of the schema object
    start = html.rfind('{', 0, idx)
    if start == -1:
        return None

    # Match balanced braces to find the exact end of the JSON object
    count = 0
    end = -1
    for i in range(start, min(len(html), start + 12000)):
        if html[i] == '{':
            count += 1
        elif html[i] == '}':
            count -= 1
            if count == 0:
                end = i + 1
                break

    if end == -1:
        return None

    raw_block = html[start:end]

    # Repeatedly unescape until clean JSON is obtained
    clean = raw_block
    for _ in range(3):
        try:
            return json.loads(clean)
        except Exception:
            try:
                clean = clean.encode('utf-8').decode('unicode_escape')
            except Exception:
                break

    try:
        return json.loads(clean)
    except Exception:
        return None

def classify_event_type(name: str, description: str, attendance_mode: str) -> str:
    text = f"{name} {description}".lower()
    
    if any(k in text for k in ['exhibition', 'expo', 'tradeshow', 'trade show', 'fair']):
        return "Exhibition / Expo"
    if any(k in text for k in ['conference', 'convention', 'symposium', 'congress', 'colloquium']):
        return "Conference"
    if any(k in text for k in ['summit']):
        return "Summit"
    if any(k in text for k in ['webinar', 'virtual event']) or "online" in attendance_mode.lower():
        return "Webinar / Virtual"
    if any(k in text for k in ['festival', 'gala', 'celebration', 'parade']):
        return "Festival / Celebration"
    if any(k in text for k in ['workshop', 'masterclass', 'institute', 'training', 'seminar', 'course']):
        return "Workshop / Seminar"
    if any(k in text for k in ['meeting', 'roundtable', 'forum', 'reception', 'networking']):
        return "Meeting / Networking"
    
    return "Event / General"

def parse_cvent_page(url: str, timeout: int = 12) -> Dict[str, Any]:
    """
    Fetches and parses a single Cvent event page.
    """
    parsed_u = urlparse(url)
    if parsed_u.scheme not in ('http', 'https'):
        return {
            "event_name": "Error fetching page",
            "event_type": "Error",
            "start_date": "",
            "end_date": "",
            "venue_name": "",
            "street_address": "",
            "city": "",
            "state_region": "",
            "country": "",
            "attendance_type": "Unknown",
            "organizer": "",
            "client_domain": parsed_u.netloc,
            "resolved_url": url,
            "cvent_url": url,
            "description_snippet": "",
            "error": f"Invalid scheme: {parsed_u.scheme}"
        }

    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
    )
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resolved_url = resp.geturl()
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return {
            "event_name": "Error fetching page",
            "event_type": "Error",
            "start_date": "",
            "end_date": "",
            "venue_name": "",
            "street_address": "",
            "city": "",
            "state_region": "",
            "country": "",
            "attendance_type": "Unknown",
            "organizer": "",
            "client_domain": urlparse(url).netloc,
            "resolved_url": url,
            "cvent_url": url,
            "description_snippet": "",
            "error": str(e)
        }

    # Extract Page Title Fallback
    title_m = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
    raw_title = title_m.group(1).strip() if title_m else ""
    clean_title = re.sub(r'\s*[\-–|]\s*(?:Online registration by Cvent|Cvent).*$', '', raw_title, flags=re.I).strip()
    clean_title = re.sub(r'^(?:Home|Summary|Welcome|Personal Information)\s*[\-–|]\s*', '', clean_title, flags=re.I).strip()

    # Extract Meta Description
    desc_m = re.search(r'<meta\s+(?:name|property)=["\'](?:description|og:description)["\']\s+content=["\'](.*?)["\']', html, re.I | re.S)
    description = desc_m.group(1).strip() if desc_m else ""

    # Schema JSON extraction
    schema = extract_schema_json(html) or {}

    raw_event_name = schema.get("name") or clean_title or raw_title
    event_name = html.unescape(raw_event_name).strip() if raw_event_name else ""
    start_date = schema.get("startDate") or ""
    end_date = schema.get("endDate") or ""
    
    # Location
    location_data = schema.get("location", {})
    venue_name = ""
    street_address = ""
    city = ""
    region = ""
    country = ""
    
    if isinstance(location_data, dict):
        venue_name = html.unescape(str(location_data.get("name", ""))).strip()
        addr = location_data.get("address", {})
        if isinstance(addr, dict):
            street_address = html.unescape(str(addr.get("streetAddress", ""))).strip()
            city = html.unescape(str(addr.get("addressLocality", ""))).strip()
            region = html.unescape(str(addr.get("addressRegion", ""))).strip()
            country = html.unescape(str(addr.get("addressCountry", ""))).strip()
    elif isinstance(location_data, str):
        venue_name = html.unescape(location_data).strip()

    # Attendance mode
    attendance_mode = str(schema.get("eventAttendanceMode", ""))
    if "Online" in attendance_mode:
        attendance_type = "Virtual / Online"
        if not venue_name:
            venue_name = "Online Event"
    elif "Offline" in attendance_mode:
        attendance_type = "In-Person"
    elif "Mixed" in attendance_mode:
        attendance_type = "Hybrid"
    else:
        attendance_type = "In-Person" if venue_name else "Not Specified"

    # Client / Organizer Identification
    organizer = ""
    org_data = schema.get("organizer", {})
    if isinstance(org_data, dict):
        organizer = html.unescape(str(org_data.get("name", ""))).strip()
    elif isinstance(org_data, str):
        organizer = html.unescape(org_data).strip()

    parsed_domain = urlparse(resolved_url).netloc.lower()
    if not organizer and parsed_domain != "web.cvent.com":
        domain_parts = parsed_domain.split('.')
        if len(domain_parts) >= 2:
            domain_name = domain_parts[-2].upper()
            if domain_name == "AARP":
                organizer = "AARP"
            elif domain_name == "AMERICANBAR":
                organizer = "American Bar Association (ABA)"
            else:
                organizer = domain_name

    # From creator emails
    emails = re.findall(r'\\*"createdBy\\*":\s*\\*"([^\\"]+@[^\\"]+)\\*"', html)
    planner_email = emails[0] if emails else ""
    if not organizer and planner_email:
        domain = planner_email.split('@')[-1]
        company = domain.split('.')[0].replace('-', ' ').title()
        if company.lower() not in ['cvent', 'gmail', 'yahoo', 'outlook']:
            organizer = company

    # Event Type Classification
    event_type = classify_event_type(event_name, description, attendance_type)

    return {
        "event_name": event_name,
        "event_type": event_type,
        "start_date": start_date,
        "end_date": end_date,
        "venue_name": venue_name,
        "street_address": street_address,
        "city": city,
        "state_region": region,
        "country": country,
        "attendance_type": attendance_type,
        "organizer": organizer,
        "client_domain": parsed_domain,
        "resolved_url": resolved_url,
        "cvent_url": url,
        "description_snippet": (description[:250] + "...") if len(description) > 250 else description
    }
