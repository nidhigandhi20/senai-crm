# # backend/test.py
# import asyncio
# from classifier.engine import classify_by_message_id
# from db.database import SessionLocal

# async def test():
#     db = SessionLocal()
#     try:
#         # Test with an email that actually exists
#         result = await classify_by_message_id("msg_006", db)
        
#         if result is None:
#             print("❌ Email not found in database")
#             return
        
#         print(result.model_dump_json(indent=2))
#     finally:
#         db.close()

# if __name__ == "__main__":
#     asyncio.run(test())


import asyncio
from db.database import SessionLocal
from classifier.engine import classify_by_message_id

async def test():
    db = SessionLocal()
    for mid in ("msg_033", "msg_041"):
        r = await classify_by_message_id(mid, db)
        print(f"{mid}: {r.category} | {r.urgency} | human={r.requires_human} | citations={r.policy_citations}")
    db.close()

asyncio.run(test())