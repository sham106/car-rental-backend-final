"""Complete saved job cards and their service records in a single transaction."""

from .fleet_domain import audit, fail, find, now, touch, validate, write_resource
from .fleet_models import MaintenanceInput, ServiceJobCompletion


def complete_job(state, actor, key, payload):
    job = find(state, "service_jobs", key)
    # A retry must never create a second maintenance entry or overwrite a signed job.
    if job["status"] == "Completed":
        return job
    data = validate(ServiceJobCompletion, payload)
    if data["vehicleId"] != job["vehicleId"]:
        fail("Complete service for the vehicle on this job card.")
    if data["date"] < job["dateOut"]:
        fail("Completion date cannot precede the date sent.")
    if data["mileage"] < job["vehicleSnapshot"]["mileage"]:
        fail("Service mileage cannot be below the job card odometer.")
    for file_id in data["attachmentIds"]:
        file = find(state, "files", file_id)
        if file["kind"] != "document":
            fail("Attach uploaded private documents.")
    record = write_resource(
        state,
        actor,
        "maintenance",
        {k: v for k, v in data.items() if k in MaintenanceInput.model_fields},
    )
    record["serviceJobId"] = key
    for file_id in data["attachmentIds"]:
        document = write_resource(
            state,
            actor,
            "documents",
            {
                "vehicleId": job["vehicleId"],
                "documentType": "Service Invoice",
                "title": f"{job['reference']} — signed card / invoice",
                "fileId": file_id,
                "notes": f"Service job {job['reference']}",
            },
        )
        document["serviceJobId"] = key
    job.update(status="Completed", completedAt=now(), maintenanceId=record["id"], completion=data)
    touch(job)
    audit(state, actor, "Completed", "service_jobs", job)
    return job
