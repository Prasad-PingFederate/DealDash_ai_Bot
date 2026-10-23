from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

@dataclass
class Deal:
    id: str
    retailer: str
    title: str
    url: str
    description: Optional[str] = None
    current_price: Optional[str] = None
    original_price: Optional[str] = None
    discount_percent: Optional[int] = None
    category: Optional[str] = None
    image_url: Optional[str] = None
    expires_at: Optional[str] = None

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= exp
        except ValueError:
            return False

    def is_valid(self) -> bool:
        if not self.title or not self.url or not self.retailer:
            return False
        if not self.url.lower().startswith("https://"):
            return False
        if self.is_expired():
            return False
        return True
