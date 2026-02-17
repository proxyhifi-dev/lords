import json
import os
from datetime import datetime, timedelta

TOKEN_FILE = "token.json"

class TokenStore:

    @staticmethod
    def save(data: dict):
        data["created_at"] = datetime.utcnow().isoformat()
        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f)

    @staticmethod
    def load():
        if not os.path.exists(TOKEN_FILE):
            return None
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)

    @staticmethod
    def is_expired(token_data):
        created = datetime.fromisoformat(token_data["created_at"])
        return datetime.utcnow() > created + timedelta(hours=23)
