from sqlalchemy import create_engine, text
engine = create_engine('postgresql://postgres:Zlendo_07@localhost:5432/postgres')
with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
    conn.execute(text('CREATE DATABASE "4k_render"'))
    print("Database 4k_render created successfully.")
