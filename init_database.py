"""
Database initialization script.

Behavior:
- Connects to MySQL server
- Creates database if it does NOT exist
- Connects to the database
- Creates all tables defined in SQLAlchemy models
- Safe to run multiple times
"""

from app.database import session, models
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url


def init_database():
    print("=" * 60)
    print("DATABASE INITIALIZATION")
    print("=" * 60)
    print(f"Database URL: {session.DATABASE_URL}")

    try:
        # --------------------------------------------------
        # Parse DB URL to get database name
        # --------------------------------------------------
        db_url = make_url(session.DATABASE_URL)
        database_name = db_url.database

        # --------------------------------------------------
        # Connect WITHOUT database (to create DB if missing)
        # --------------------------------------------------
        # For postgresql, connect to default 'postgres' database
        if db_url.drivername.startswith("postgresql"):
            server_url = db_url.set(database="postgres")
        else:
            server_url = db_url.set(database=None)
            
        server_engine = create_engine(server_url)

        print("\nChecking database server connection...")

        # In PostgreSQL, we can't create databases inside a transaction block using default isolation level
        with server_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            print("✅ Connected to database server")

            # --------------------------------------------------
            # Create database if it doesn't exist
            # --------------------------------------------------
            print(f"\nEnsuring database exists: `{database_name}`")
            if db_url.drivername.startswith("postgresql"):
                # Check if DB exists in Postgres
                result = conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname='{database_name}'")).scalar()
                if not result:
                    conn.execute(text(f'CREATE DATABASE "{database_name}"'))
            else:
                conn.execute(
                    text(
                        f"""
                        CREATE DATABASE IF NOT EXISTS `{database_name}`
                        CHARACTER SET utf8mb4
                        COLLATE utf8mb4_unicode_ci
                        """
                    )
                )
            print(f"✅ Database `{database_name}` is ready")

        # --------------------------------------------------
        # Now connect to the actual database
        # --------------------------------------------------
        print("\nConnecting to target database...")
        engine = session.engine

        with engine.connect() as conn:
            if db_url.drivername.startswith("postgresql"):
                result = conn.execute(text("SELECT current_database()"))
            else:
                result = conn.execute(text("SELECT DATABASE()"))
            
            active_db = result.scalar()
            print(f"✅ Connected to database: {active_db}")

        # --------------------------------------------------
        # Inspect existing tables
        # --------------------------------------------------
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()

        print(f"\nExisting tables: {len(existing_tables)}")
        for table in existing_tables:
            print(f"  - {table}")

        # --------------------------------------------------
        # Create tables (safe / idempotent)
        # --------------------------------------------------
        print("\nCreating/updating tables...")
        models.Base.metadata.create_all(bind=engine)

        # --------------------------------------------------
        # Verify tables after creation
        # --------------------------------------------------
        inspector = inspect(engine)
        all_tables = inspector.get_table_names()

        print(f"\n✅ Database initialization completed!")
        print(f"Total tables: {len(all_tables)}")

        new_tables = set(all_tables) - set(existing_tables)
        if new_tables:
            print("\n📝 Newly created tables:")
            for table in sorted(new_tables):
                print(f"  + {table}")
        else:
            print("\n📝 All tables already existed")

    except Exception as e:
        print(f"\n❌ Database initialization failed: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    init_database()
