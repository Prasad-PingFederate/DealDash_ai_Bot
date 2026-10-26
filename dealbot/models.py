import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

def parse_rupees(value: Optional[str]) -> Optional[int]:
    """Pull a rupee amount out of strings like '₹1,499' or 'Rs. 799'."""
    if value is None:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        amount = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0 or amount > 500_000:
        return None
    return int(round(amount))


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
    coupon_code: Optional[str] = None
    bank_offer: Optional[str] = None
    price_note: Optional[str] = None
    checked: bool = False

    def savings_rupees(self) -> Optional[int]:
        price = parse_rupees(self.current_price)
        mrp = parse_rupees(self.original_price)
        if price is None or mrp is None or mrp <= price:
            return None
        return mrp - price

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
