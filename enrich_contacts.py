"""
High-fidelity Contact & Planner Enrichment Module for Cvent.
Strict, deterministic extraction of:
  - creator_email (from internal 'createdBy' & 'lastModifiedBy' JSON keys)
  - general_emails (from mailto, JSON contact fields, and text regex)
  - contact_name (from 'ContactPlanner' and explicit firstName/lastName blocks)
  - creator_phone & general_phone (from 'ContactPlanner.phoneNumber' and tel: links)
  - contact_type ('In-House' vs '3rd-Party Agency')
"""

import urllib.request
import ssl
import re
from urllib.parse import urlparse

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
}

DISCARD_NAMES = {
    'planner first', 'planner last', 'planner first planner last',
    'first name', 'last name', 'first last', 'test planner', 'admin'
}

DISCARD_EMAIL_DOMAINS = set()

AGENCY_KEYWORDS = [
    'meeting', 'event', 'agency', 'production', 'experience', 'group',
    'pco', 'consult', 'management', 'direct', 'solution', 'association-mgmt'
]

KNOWN_AGENCIES = [
    'demarsemeetings', '300brand', 'fanfares', 'conferencedirect',
    'helmsbriscoe', 'executivevents', 'powereventgroup', 'signalhouseevents',
    'jkingconsult', 'factory73', 'micepodium', 'premiogroup'
]


def extract_brand_tokens(s: str) -> set:
    if not s:
        return set()
    s = s.lower()
    s = re.sub(r'^(https?://|www\.)', '', s)
    s = s.split('.')[0] if '.' in s else s
    parts = re.split(r'[-_0-9\s]+', s)
    generic = {'www', 'events', 'event', 'cvent', 'cventevents', 'conference', 'summit', 'annual', 'meeting', 'portal', 'registration', 'attend', 'group', 'intl', 'inc', 'corp'}
    return {p for p in parts if len(p) >= 2 and p not in generic}


def determine_contact_type(email: str, name: str, org: str, client_domain: str) -> str:
    """
    Strict, deterministic classification of In-House vs 3rd-Party Agency.
    Rule: Default is ALWAYS In-House.
    An event is ONLY classified as a 3rd-Party Agency if the creator's email 
    or organizer is positively identified as an external event production firm / PCO.
    Never classify corporate, university (.edu), non-profit (.org), or hospital emails as an agency.
    """
    if not email and not name:
        return ""
    if not email:
        return "In-House"

    emails_list = [e.strip().lower() for e in email.split(',') if '@' in e and '.' in e]
    if not emails_list:
        return "In-House"

    for em in emails_list:
        domain = em.split('@')[-1]
        company = domain.split('.')[0]

        # 1. Known, verified event production and management agencies
        if any(ag in company for ag in KNOWN_AGENCIES):
            return "3rd-Party Agency"

        # 2. Specific event production agency keywords in the company domain
        if any(k in company for k in ['demarsemeetings', 'meetings', 'eventgroup', 'eventagency', 'eventproduction', 'associationsmgt', '-pco', '_pco', 'pco-']):
            return "3rd-Party Agency"

    return "In-House"


def clean_phone(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r'[^\d]', '', phone)
    if len(digits) < 7 or len(digits) > 15:
        return ""
    return phone.strip()


def process_single_event(row):
    """
    Inspects Cvent event URL and fetches details, extracting creator emails,
    contact names, phone numbers, and agency classification.
    """
    uuid = row.get('event_uuid', '')
    url = row.get('resolved_url') or row.get('cvent_url', '')
    org = row.get('organizer', '')
    client_domain = row.get('client_domain', '')

    contact_name = ""
    contact_phone = ""
    contact_email = ""
    creator_email = ""
    general_emails = ""
    creator_phone = ""
    general_phone = ""
    contact_type = ""

    if not url:
        return {
            "event_uuid": uuid, "contact_name": "", "creator_email": "",
            "general_emails": "", "contact_email": "", "creator_phone": "",
            "general_phone": "", "contact_phone": "", "contact_type": ""
        }

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')

        # 1. Names Extraction (ContactPlanner, all firstName/lastName pairs)
        names = []
        cp_blocks = re.findall(r'\\*"ContactPlanner\\*":\s*\{([^}]+)\}', html)
        for block in cp_blocks:
            fn = re.search(r'\\*"firstName\\*":\s*\\*"([^\\"]*)\\*"', block)
            ln = re.search(r'\\*"lastName\\*":\s*\\*"([^\\"]*)\\*"', block)
            n = f"{fn.group(1) if fn else ''} {ln.group(1) if ln else ''}".strip()
            if n and n.lower() not in ['first last', 'planner first planner last', 'first name last name']:
                if n not in names:
                    names.append(n)

        pairs = re.findall(r'\\*"firstName\\*":\s*\\*"([^\\"]+)\\*",\s*\\*"lastName\\*":\s*\\*"([^\\"]+)\\*"', html)
        for f, l in pairs:
            n = f"{f} {l}".strip()
            if n and len(n) < 50 and n.lower() not in DISCARD_NAMES and n not in names:
                names.append(n)

        # 2. Phone Extraction: Separation between Creator Phone (ContactPlanner) and General Phone
        creator_phones_list = []
        for block in cp_blocks:
            ph = re.search(r'\\*"phoneNumber\\*":\s*\\*"([^\\"]*)\\*"', block)
            if ph and ph.group(1):
                cp = clean_phone(ph.group(1))
                if cp and cp not in creator_phones_list:
                    creator_phones_list.append(cp)

        tels = re.findall(r'tel:([+0-9\(\)\s\-\.]{7,25})', html)
        json_phones = re.findall(r'\\*"(?:phone|telephone|contactPhone|supportPhone|mobile)\\*":\s*\\*"([^\\"]{7,25})\\*"', html)
        general_phones_list = []
        for t in (tels + json_phones):
            cp = clean_phone(t)
            if cp and cp not in creator_phones_list and cp not in general_phones_list:
                general_phones_list.append(cp)

        all_phones = creator_phones_list + general_phones_list

        # 3. Emails Extraction: Distinct separation between Creator and General emails
        created_by = re.findall(r'\\*"createdBy\\*":\s*\\*"([^\\"]+@[^\\"]+)\\*"', html)
        last_modified = re.findall(r'\\*"lastModifiedBy\\*":\s*\\*"([^\\"]+@[^\\"]+)\\*"', html)

        creator_emails_list = []
        for e in (created_by + last_modified):
            ec = e.strip().lower()
            if '@' in ec and '.' in ec.split('@')[-1] and ec not in creator_emails_list:
                creator_emails_list.append(ec)

        # General emails from mailto, JSON fields, and plain text
        mailtos = re.findall(r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', html)
        json_emails = re.findall(r'\\*"(?:email|supportEmail|contactEmail)\\*":\s*\\*"([^\\"]+@[^\\"]+)\\*"', html)
        plain_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', html)

        general_emails_list = []
        for e in (mailtos + json_emails + plain_emails):
            ec = e.strip().lower()
            if any(bad in ec for bad in ['.png', '.jpg', '.jpeg', '.svg', '.gif', 'w3.org', 'schema.org']):
                continue
            if '@' in ec and '.' in ec.split('@')[-1]:
                if ec not in creator_emails_list and ec not in general_emails_list:
                    general_emails_list.append(ec)

        all_clean_emails = creator_emails_list + general_emails_list

        for em in all_clean_emails:
            user = em.split('@')[0]
            if '.' in user:
                parts = user.split('.')
                if len(parts) == 2 and all(p.isalpha() and len(p) >= 2 for p in parts):
                    name_cand = f"{parts[0].capitalize()} {parts[1].capitalize()}"
                    if name_cand not in names:
                        names.append(name_cand)

        contact_name = ", ".join(names)
        creator_email = ", ".join(creator_emails_list)
        general_emails = ", ".join(general_emails_list)
        contact_email = ", ".join(all_clean_emails)
        creator_phone = ", ".join(creator_phones_list)
        general_phone = ", ".join(general_phones_list)
        contact_phone = ", ".join(all_phones)
        contact_type = determine_contact_type(contact_email, contact_name, org, client_domain)

    except Exception:
        pass

    return {
        "event_uuid": uuid,
        "contact_name": contact_name,
        "creator_email": creator_email,
        "general_emails": general_emails,
        "contact_email": contact_email,
        "creator_phone": creator_phone,
        "general_phone": general_phone,
        "contact_phone": contact_phone,
        "contact_type": contact_type
    }
