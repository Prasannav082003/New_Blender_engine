from sqlalchemy import JSON
import enum
from sqlalchemy import (Column, Integer, String, Float, Enum, Boolean,
                        ForeignKey, Text)
from sqlalchemy.orm import relationship
from passlib.context import CryptContext
from .session import Base
from sqlalchemy import Column, Integer, String, Text, Float, DateTime
from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT

# --- Password Hashing Context ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- Enums for Consistency ---
class MountType(str, enum.Enum):
    floor_mount = "floor_mount"
    wall_mount = "wall_mount"
    ceiling_mount = "ceiling_mount"
    general = "general"

# --- Main Tables ---
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    hashed_api_key = Column(String(255), unique=True, index=True, nullable=True)

    styles = relationship("Style", back_populates="owner", cascade="all, delete-orphan")
    assets = relationship("Asset", back_populates="owner", cascade="all, delete-orphan")
    textures = relationship("Texture", back_populates="owner", cascade="all, delete-orphan")

    def verify_password(self, plain_password):
        return pwd_context.verify(plain_password, self.hashed_password)
    
    def verify_api_key(self, plain_api_key):
        # We can reuse the same password context for hashing/verifying
        return pwd_context.verify(plain_api_key, self.hashed_api_key)

    @staticmethod
    def get_api_key_hash(api_key):
        return pwd_context.hash(api_key)

    @staticmethod
    def get_password_hash(password):
        return pwd_context.hash(password)

class Style(Base):
    __tablename__ = "styles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    description = Column(Text)
    
    # These fields allow for styles that are "assets only" or "textures only"
    assets_only = Column(Boolean, default=False)
    textures_only = Column(Boolean, default=False)
    
    # Store lists as JSON strings for simplicity in standard SQL
    mandatory_assets = Column(Text, default='[]')
    optional_assets = Column(Text, default='[]')
    texture_rules = Column(Text, default='{}') # e.g., '{"wall": "texture_name_1", "floor": "texture_name_2"}'
    
    user_id = Column(Integer, ForeignKey("users.id"))
    # ✅ NEW
    is_public = Column(Boolean, default=False, nullable=False)  # shared style toggle
    shared_by = Column(String(255), nullable=True)  
    
    owner = relationship("User", back_populates="styles")
    rules = relationship("StyleRule", back_populates="style", cascade="all, delete-orphan")
    asset_relationships = relationship(
        "AssetRelationship",
        back_populates="style",
        cascade="all, delete-orphan",
        passive_deletes=True  # Recommended for performance with ON DELETE CASCADE
    )

class StyleRule(Base):
    __tablename__ = "style_rules"

    id = Column(Integer, primary_key=True, index=True)
    style_id = Column(Integer, ForeignKey("styles.id"), nullable=False)
    room_type = Column(String(255), nullable=False)
    min_area_m2 = Column(Float, nullable=False)
    max_area_m2 = Column(Float, nullable=False)
    
    style = relationship("Style", back_populates="rules")

class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    
    # --- EXTENDED FIELDS AS PER R&D PLAN ---
    category = Column(String(255), default="Uncategorized") # e.g., 'furniture', 'decor', 'lighting'
    mount_type = Column(Enum(MountType), nullable=False)
    width = Column(Float, nullable=False)
    depth = Column(Float, nullable=False)
    height = Column(Float, nullable=False)
    altitude = Column(Float, default=0)
    rotation_standard = Column(String(50), default='-Y') # Crucial for consistency
    obj_path = Column(String(512))  # Path/URL to the .obj file
    mtl_path = Column(String(512))  # Path/URL to the .mtl file
    thumbnail_path = Column(String(512))
    svg_path = Column(String(512), nullable=True) # URL to the 2D SVG file
    tags = Column(Text, default='[]') # For filtering and searching
    is_active = Column(Boolean, default=True) # To enable/disable assets
    
    user_id = Column(Integer, ForeignKey("users.id"))
    
    owner = relationship("User", back_populates="assets")

# --- NEW TABLES AS PER R&D PLAN ---
class Texture(Base):
    __tablename__ = "textures"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    category = Column(String(50))  # e.g., 'wall', 'floor', 'ceiling', 'furniture'
    thumbnail_path = Column(String(512))
    # Storing multiple URLs as a JSON string in a TEXT field
    texture_urls = Column(Text) # Simplified to one field for POC
    scale = Column(String(50))
    finish_type = Column(String(50))
    tags = Column(Text)
    is_active = Column(Boolean, default=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    owner = relationship("User", back_populates="textures")

class PlacementRule(Base):
    __tablename__ = "placement_rules"

    id = Column(Integer, primary_key=True, index=True)
    mount_type = Column(Enum(MountType), unique=True, nullable=False) # One rule per mount type
    rule_definition = Column(JSON, default='{}') # JSON with specific logic params (e.g., '{"wall_offset_cm": 5}')
    prevent_overlap = Column(Boolean, default=True)
    respect_openings = Column(Boolean, default=True)
    randomize_placement = Column(Boolean, default=True)

class AssetRelationship(Base):
    __tablename__ = "asset_relationships"

    id = Column(Integer, primary_key=True, index=True)
    
    # Defines which style this relationship belongs to
    style_id = Column(Integer, ForeignKey("styles.id"), nullable=False)
    
    # The 'parent' is the asset that is placed first (e.g., the bed)
    parent_asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    
    # The 'child' is the asset placed relative to the parent (e.g., the bedside table)
    child_asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    
    # The type of relationship
    relation_type = Column(String(50), nullable=False) # e.g., 'on_top_of', 'left_of'
    
    # Optional offsets in cm for fine-tuning
    offset_x = Column(Float, default=0.0)
    offset_y = Column(Float, default=0.0)
    offset_z = Column(Float, default=0.0)
    
    # --- BEHAVIOR FLAGS ---
    # If true, the child will match the parent's rotation
    inherit_rotation = Column(Boolean, default=True) 
    
    # Relationships back to other tables
    style = relationship("Style", back_populates="asset_relationships")
    parent_asset = relationship("Asset", foreign_keys=[parent_asset_id])
    child_asset = relationship("Asset", foreign_keys=[child_asset_id])

class RequestLog(Base):
    __tablename__ = "request_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp_utc = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Request Info
    user_email = Column(String(255), nullable=True) # Who made the request
    http_method = Column(String(10), nullable=False) # e.g., POST
    path = Column(String(512), nullable=False) # e.g., /engine/generate-inspiration-zealty
    query_params = Column(Text, nullable=True)
    request_body = Column(Text().with_variant(LONGTEXT, 'mysql'), nullable=True)
    client_host = Column(String(255), nullable=True)
    # Response Info
    status_code = Column(Integer, nullable=False) # e.g., 200 or 500
    response_body = Column(Text().with_variant(LONGTEXT, 'mysql'), nullable=True)
    duration_ms = Column(Float, nullable=False) # How long the request took

