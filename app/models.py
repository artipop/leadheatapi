from datetime import datetime, UTC
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, DateTime, Text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(30))
    fullname: Mapped[Optional[str]]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r}, fullname={self.fullname!r})"


class DublgisCompany(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    dublgis_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    region_id: Mapped[int] = mapped_column(Integer, index=True)
    rubric_ids: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    purpose_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    building_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    item_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_deleted: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    site_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    item_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sites_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    crawled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_scope: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    inn: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ogrn: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ogrnip: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    phones: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    emails: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    pricing_items: Mapped[list["Pricing"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    specialists: Mapped[list["Specialist"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    social_profiles: Mapped[list["CompanySocialProfile"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )
    branches: Mapped[list["Branch"]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
    )


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="branches")


class Pricing(Base):
    __tablename__ = "pricing"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text)
    price_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    price_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="pricing_items")


class Specialist(Base):
    __tablename__ = "specialists"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    role: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="specialists")


class CompanySocialProfile(Base):
    __tablename__ = "company_social_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id", ondelete="CASCADE"), index=True)

    hh_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tg_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vk_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dzen_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rutube_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instagram_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    youtube_profile: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="social_profiles")
