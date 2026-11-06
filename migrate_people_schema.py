"""
Database migration script to add contact fields to 'cvent' and create 'cvent_people'.
"""
from db_compat import get_db_connection, safe_alter_add_column, adapt_create_table_sql

def run_migration():
    conn = get_db_connection()
    print("Migrating 'cvent' table columns...")
    safe_alter_add_column(conn, 'cvent', 'contact_name', "TEXT DEFAULT ''")
    safe_alter_add_column(conn, 'cvent', 'contact_phone', "TEXT DEFAULT ''")
    safe_alter_add_column(conn, 'cvent', 'general_emails', "TEXT DEFAULT ''")
    safe_alter_add_column(conn, 'cvent', 'contact_type', "TEXT DEFAULT 'In-House'")

    print("Creating 'cvent_people' table...")
    conn.execute(adapt_create_table_sql("""
        CREATE TABLE IF NOT EXISTS cvent_people (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL REFERENCES cvent(id) ON DELETE CASCADE,
            organizer_name TEXT DEFAULT '',
            contact_name TEXT DEFAULT '',
            designation TEXT DEFAULT '-',
            is_creator_email BOOLEAN DEFAULT FALSE,
            phone TEXT DEFAULT '-',
            linkedin_url TEXT DEFAULT '-',
            email TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            scraper_source TEXT DEFAULT 'Cvent',
            contact_type TEXT DEFAULT 'In-House',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_event_person_email UNIQUE (event_id, email)
        )
    """))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_email ON cvent_people(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_event_id ON cvent_people(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cvent_people_is_creator ON cvent_people(is_creator_email)")
    conn.commit()

    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='cvent_people'")
    cols = [r['column_name'] for r in cur.fetchall()]
    print("Migration successful! 'cvent_people' columns:", cols)

    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='cvent'")
    cvent_cols = [r['column_name'] for r in cur.fetchall()]
    print("'cvent' columns count:", len(cvent_cols))
    conn.close()

if __name__ == "__main__":
    run_migration()
