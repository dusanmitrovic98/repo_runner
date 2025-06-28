import os
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, List

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from cryptography.fernet import Fernet

load_dotenv()

# Initialize logging
logger = logging.getLogger("database")
logger.setLevel(logging.INFO)

class DataAccessLayer:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_name: Optional[str] = None):
        if self._initialized:
            return
        self._initialized = True
        
        # Initialize MongoDB connection
        self.mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.db_name = db_name or os.getenv("MONGO_DB_NAME", "test")
        self.client = None
        self.db = None
        
        # Encryption setup
        self.cipher = self._get_cipher()
        
        # In-memory cache
        self.cache = {}
        self.cache_ttl = 60  # seconds
    
    def _get_cipher(self):
        """Initialize encryption cipher"""
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            key = Fernet.generate_key().decode()
            os.environ["ENCRYPTION_KEY"] = key
            logger.warning("Generated new encryption key")
        return Fernet(key.encode())
    
    async def connect(self):
        """Establish database connection"""
        if self.client is None:
            self.client = AsyncIOMotorClient(self.mongo_uri)
            self.db = self.client[self.db_name]
            logger.info(f"Database connection established (db: {self.db_name})")
            await self.initialize_indexes()
    
    async def close(self):
        """Close database connection"""
        if self.client:
            self.client.close()
            self.client = None
            logger.info("Database connection closed")
    
    # Unified CRUD operations
    async def create(self, collection: str, data: dict) -> str:
        """Create a new document"""
        await self.connect()
        data = self._process_data(data, encrypt=True)
        result = await self.db[collection].insert_one(data)
        doc_id = str(result.inserted_id)
        logger.debug(f"Created document in {collection}: {doc_id}")
        return doc_id
    
    async def get(self, collection: str, query: dict, use_cache: bool = True) -> Optional[dict]:
        """Get a single document"""
        await self.connect()
        
        # Try cache first
        cache_key = f"{collection}:{json.dumps(query, sort_keys=True)}"
        if use_cache and cache_key in self.cache:
            return self.cache[cache_key]
        
        doc = await self.db[collection].find_one(query)
        if doc:
            doc = self._process_data(doc, decrypt=True)
            doc['_id'] = str(doc['_id'])  # Convert ObjectId to string
            self.cache[cache_key] = doc
            return doc
        return None
    
    async def update(self, collection: str, query: dict, update: dict, upsert: bool = False) -> Optional[dict]:
        """Update a document"""
        await self.connect()
        update = self._process_data(update, encrypt=True)
        result = await self.db[collection].find_one_and_update(
            query,
            {'$set': update},
            return_document=ReturnDocument.AFTER,
            upsert=upsert
        )
        if result:
            result = self._process_data(result, decrypt=True)
            result['_id'] = str(result['_id'])
            self._clear_cache(collection, query)
            return result
        return None
    
    async def delete(self, collection: str, query: dict) -> bool:
        """Delete a document"""
        await self.connect()
        result = await self.db[collection].delete_one(query)
        self._clear_cache(collection, query)
        return result.deleted_count > 0
    
    async def find(self, collection: str, query: dict = {}, limit: int = 100) -> List[dict]:
        """Find multiple documents"""
        await self.connect()
        cursor = self.db[collection].find(query).limit(limit)
        results = []
        async for doc in cursor:
            doc = self._process_data(doc, decrypt=True)
            doc['_id'] = str(doc['_id'])
            results.append(doc)
        return results
    
    # Specialized methods
    async def add_to_array(self, collection: str, query: dict, array_field: str, value: Any) -> bool:
        """Add item to an array field"""
        await self.connect()
        result = await self.db[collection].update_one(
            query,
            {'$addToSet': {array_field: value}}
        )
        self._clear_cache(collection, query)
        return result.modified_count > 0
    
    async def increment(self, collection: str, query: dict, field: str, amount: int) -> bool:
        """Increment a numeric field"""
        await self.connect()
        result = await self.db[collection].update_one(
            query,
            {'$inc': {field: amount}}
        )
        self._clear_cache(collection, query)
        return result.modified_count > 0
    
    async def replace_all(self, collection: str, data: list):
        """Replace all documents in a collection"""
        await self.connect()
        await self.db[collection].delete_many({})
        if data:
            await self.db[collection].insert_many(data)
    
    # Helper methods
    def _process_data(self, data: dict, encrypt: bool = False, decrypt: bool = False) -> dict:
        """Handle encryption/decryption of sensitive fields"""
        processed = {}
        for key, value in data.items():
            if key in ['api_key', 'token']:  # Sensitive fields
                if encrypt:
                    processed[key] = self.cipher.encrypt(value.encode()).decode()
                elif decrypt and isinstance(value, str):
                    try:
                        processed[key] = self.cipher.decrypt(value.encode()).decode()
                    except Exception as e:
                        logger.error(f"Decryption failed for {key}: {e}")
                        processed[key] = value
                else:
                    processed[key] = value
            else:
                processed[key] = value
        return processed
    
    def _clear_cache(self, collection: str, query: dict):
        """Clear cache entries matching pattern"""
        pattern = f"{collection}:{json.dumps(query, sort_keys=True)}"
        keys = [k for k in self.cache.keys() if k.startswith(pattern)]
        for key in keys:
            del self.cache[key]
    
    async def initialize_indexes(self):
        """Create essential database indexes for your project"""
        try:
            # Index for URLs collection: ensure 'url' is unique
            await self.db.urls.create_index("url", unique=True)
            # Index for ping history collection (if you migrate it):
            # await self.db.ping_history.create_index("url")
            logger.info("Database indexes initialized for URLs.")
        except Exception as e:
            logger.error(f"Index initialization failed: {e}")

# Global database instance
# To use a custom db name: db = DataAccessLayer(db_name="mydb")

db = DataAccessLayer(db_name="terminal")