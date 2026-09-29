import os
import argparse
from sqlalchemy import create_engine, MetaData

def migrate(source_url, target_url):
    print(f"Connecting to source database...")
    source_engine = create_engine(source_url)
    
    print(f"Connecting to target database...")
    target_engine = create_engine(target_url)

    metadata = MetaData()
    
    print("Reflecting metadata from source database...")
    metadata.reflect(bind=source_engine)

    print("Creating tables in target database using app models...")
    # Import the models to ensure tables are created with proper SQLAlchemy mappings, not reflected MySQL schemas
    from app.database import models
    models.Base.metadata.create_all(bind=target_engine)

    # Disable foreign key checks for Postgres during migration if possible

    # We will just copy the data in the order of foreign key dependencies (sorted_tables handles this)
    for table in metadata.sorted_tables:
        print(f"Migrating table: {table.name}...")
        
        with source_engine.connect() as src_conn:
            with target_engine.begin() as tgt_conn: # begin() auto-commits on success
                # Select all data
                result = src_conn.execute(table.select()).fetchall()
                if result:
                    # Convert result to list of dicts for insert
                    data = [dict(row._mapping) for row in result]
                    
                    # Insert in chunks to avoid memory issues
                    chunk_size = 1000
                    for i in range(0, len(data), chunk_size):
                        chunk = data[i:i + chunk_size]
                        tgt_conn.execute(table.insert(), chunk)
                    print(f"  -> Migrated {len(data)} rows.")
                else:
                    print(f"  -> Table is empty.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate from MySQL to PostgreSQL")
    parser.add_argument("--source", default="mysql+pymysql://root:Zlendo_07@localhost:3306/4k_render?charset=utf8mb4", help="Source MySQL DATABASE_URL")
    # Change target to your actual postgres credentials
    parser.add_argument("--target", default="postgresql://postgres:Zlendo_07@localhost:5432/4k_render", help="Target PostgreSQL DATABASE_URL")
    args = parser.parse_args()
    
    migrate(args.source, args.target)
    
    print("\n--- MIGRATION COMPLETE ---")
    print("To connect your application to PostgreSQL, update your .env file with:")
    print(f"DATABASE_URL={args.target}")
    print("\nMake sure you have installed the PostgreSQL adapter:")
    print("pip install psycopg2-binary")
