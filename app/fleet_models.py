"""Validated write contracts. Names match the existing React API contract."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Text = Annotated[str, Field(max_length=4000)]
Name = Annotated[str, Field(min_length=1, max_length=160)]
Money = Annotated[float, Field(ge=0, le=1_000_000_000, allow_inf_nan=False)]
Mileage = Annotated[int, Field(ge=0, le=10_000_000)]
Status = Literal[
    "available", "reserved", "rented", "assigned", "in_service", "compliance_hold", "inactive"
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def empty_optional(cls, value, info):
        field = cls.model_fields[info.field_name]
        if value == "" and field.default is None:
            return None
        return value


class OwnerInput(Input):
    name: Name
    contactPerson: Text = ""
    ownerType: Literal["Company", "Individual", "Partner Company", "Internal"] = "Company"
    phone: Text = ""
    email: EmailStr | None = None
    address: Text = ""
    notes: Text = ""
    bankAccount: Text = ""
    revenueSplitPercentage: float = Field(default=0, ge=0, le=100)


class CustomerInput(Input):
    firstName: Name
    lastName: Name
    phone: Name
    email: EmailStr
    country: Text = ""
    nationality: Text = ""
    address: Text = ""
    licenceNumber: Text = ""
    licenceExpiryDate: date | None = None
    licenceCountry: Text = ""
    idOrPassport: Text = ""
    rating: float = Field(default=0, ge=0, le=5)
    notes: Text = ""


class VehicleInput(Input):
    slug: str = Field(min_length=1, max_length=180, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    registrationNumber: Name
    brand: Name
    model: Name
    year: int | None = Field(default=None, ge=1950, le=2100)
    color: Text = ""
    vin: Text = ""
    engineNumber: Text = ""
    category: Name
    dailyRate: Money
    transmission: Literal["Automatic", "Manual"] = "Automatic"
    fuelType: Literal["Petrol", "Diesel", "Hybrid", "Electric"] = "Petrol"
    seats: int = Field(default=5, ge=1, le=60)
    luggageCapacity: int = Field(default=2, ge=0, le=60)
    doors: int = Field(default=4, ge=1, le=8)
    airConditioning: bool = True
    features: list[Name] = Field(default_factory=list, max_length=50)
    description: Text = ""
    photos: list[str] = Field(default_factory=list, max_length=20)
    ownerId: Name
    purchaseDate: date | None = None
    purchaseValue: Money = 0
    currentValue: Money = 0
    mileage: Mileage = 0
    operationalStatus: Status = "available"
    published: bool = False
    featured: bool = False
    nextServiceMileage: Mileage | None = None
    nextServiceDate: date | None = None
    statusChangeReason: Text = ""

    @field_validator("photos")
    @classmethod
    def safe_photos(cls, values):
        if any(
            len(v) > 2048 or not (v.startswith("https://") or v.startswith("/api/public/photos/"))
            for v in values
        ):
            raise ValueError("Upload photos or use HTTPS image URLs")
        return values


class AssignmentInput(Input):
    vehicleId: Name
    assignedTo: Name
    assignmentType: Literal["Staff", "Personal", "Company", "Temporary", "Other"]
    startDate: date
    expectedReturnDate: date
    mileageOut: Mileage
    reason: Name
    notes: Text = ""


class MaintenanceInput(Input):
    vehicleId: Name
    serviceType: Literal[
        "Routine Service",
        "Oil Change",
        "Tyres",
        "Brakes",
        "Mechanical",
        "Electrical",
        "Body Repair",
        "Inspection",
        "Other",
    ]
    date: date
    mileage: Mileage
    garage: Name
    description: Text = ""
    partsReplaced: Text = ""
    labourCost: Money = 0
    partsCost: Money = 0
    nextServiceMileage: Mileage | None = None
    nextServiceDate: date | None = None
    invoiceNumber: Text = ""
    notes: Text = ""


class ComplianceInput(Input):
    vehicleId: Name
    complianceType: Literal["Fitness Certificate", "Insurance", "MVL", "Licence"]
    company: Text = ""
    provider: Text = ""
    broker: Text = ""
    policyNumber: Text = ""
    premium: Money = 0
    issueDate: date
    expiryDate: date
    documentUrl: Text = ""
    notes: Text = ""

    @field_validator("documentUrl")
    @classmethod
    def safe_document_url(cls, value):
        if value and not value.startswith(("/api/admin/files/", "https://")):
            raise ValueError("Use an uploaded document or an HTTPS URL")
        return value


class DocumentComplianceInput(Input):
    company: Text = ""
    broker: Text = ""
    policyNumber: Text = ""
    premium: Money = 0


class DocumentInput(Input):
    vehicleId: Name
    documentType: Literal[
        "Insurance Certificate",
        "Fitness Certificate",
        "MVL",
        "Licence",
        "Purchase Document",
        "Service Invoice",
        "Inspection",
        "Rental Agreement",
        "Other",
    ]
    title: Name
    issueDate: date | None = None
    expiryDate: date | None = None
    fileId: Name
    compliance: DocumentComplianceInput | None = None
    notes: Text = ""


class SettingsInput(Input):
    companyName: Name = "Oceane Car Rental"
    brn: Text = ""
    vatNumber: Text = ""
    phone: Text = ""
    email: EmailStr | None = None
    headquarters: Text = ""
    defaultDeposit: Money = 15000
    airportDeliveryFee: Money = 0
    hotelDeliveryFee: Money = 450
    serviceIntervalKm: int = Field(default=10000, ge=100, le=100000)
    complianceNoticeDays: int = Field(default=30, ge=1, le=365)
    currencySymbol: Literal["Rs (MUR)"] = "Rs (MUR)"
    vatRate: float = Field(default=15, ge=0, le=100)


class CategoryInput(Input):
    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    name: Name
    tagline: Text = ""
    description: Text = ""
    representativePhoto: Text = ""


class LocationInput(Input):
    name: Name
    area: Text = ""
    pickupFee: Money = 0
    dropoffFee: Money = 0
    address: Text = ""
    isPopular: bool = False


class GuestCustomer(Input):
    firstName: Name
    lastName: Name
    phone: Name
    email: EmailStr
    country: Name
    specialRequest: Text = ""


class BookingInput(Input):
    vehicleId: Name
    pickupLocationId: Name
    returnLocationId: Name
    pickupDate: date
    returnDate: date
    customer: GuestCustomer
    idempotencyKey: str = Field(min_length=16, max_length=128)


class CheckoutInput(Input):
    mileageOut: Mileage
    fuelLevelOut: Name
    conditionNotesOut: Text = ""
    damageNotesOut: Text = ""
    checkoutPhotos: list[str] = Field(default_factory=list, max_length=20)


class CheckinInput(Input):
    mileageIn: Mileage
    fuelLevelIn: Name
    conditionNotesIn: Text = ""
    damageNotesIn: Text = ""
    checkinPhotos: list[str] = Field(default_factory=list, max_length=20)
    finalVehicleStatus: Literal["available", "in_service"] = "available"
    serviceReason: Text = ""


MODELS = {
    "vehicles": VehicleInput,
    "owners": OwnerInput,
    "customers": CustomerInput,
    "assignments": AssignmentInput,
    "maintenance": MaintenanceInput,
    "compliance": ComplianceInput,
    "documents": DocumentInput,
    "settings": SettingsInput,
    "categories": CategoryInput,
    "locations": LocationInput,
}
