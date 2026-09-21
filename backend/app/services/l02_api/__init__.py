"""L2 read models (S9): query helpers behind the FastAPI routers. Sync SQLAlchemy,
executed through ``AsyncSession.run_sync`` so request handlers never block."""
