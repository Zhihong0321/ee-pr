import os
import datetime
import boto3
from dotenv import load_dotenv

load_dotenv()

r2_endpoint = os.getenv("R2_ENDPOINT")
r2_access_key = os.getenv("R2_ACCESS_KEY_ID")
r2_secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
bucket_name = os.getenv("R2_BUCKET", "eternalgy-image")

s3 = boto3.client(
    "s3",
    endpoint_url=r2_endpoint,
    aws_access_key_id=r2_access_key,
    aws_secret_access_key=r2_secret_key,
    region_name="auto",
)

now = datetime.datetime.now(datetime.timezone.utc)
cutoff_days = 120
cutoff_date = now - datetime.timedelta(days=cutoff_days)

print(f"Scanning R2 bucket '{bucket_name}' for objects older than {cutoff_days} days (before {cutoff_date.isoformat()})...")

paginator = s3.get_paginator("list_objects_v2")
page_iterator = paginator.paginate(Bucket=bucket_name)

eligible_objects = []
total_objects = 0
total_bytes_scanned = 0

for page in page_iterator:
    contents = page.get("Contents", [])
    for obj in contents:
        total_objects += 1
        size = obj["Size"]
        last_modified = obj["LastModified"]
        total_bytes_scanned += size
        
        if last_modified < cutoff_date:
            age_days = (now - last_modified).days
            eligible_objects.append({
                "key": obj["Key"],
                "size": size,
                "size_mb": round(size / (1024 * 1024), 2),
                "last_modified": last_modified.isoformat(),
                "age_days": age_days,
                "etag": obj.get("ETag", "").strip('"')
            })

# Sort by filesize descending
eligible_objects.sort(key=lambda x: x["size"], reverse=True)

print(f"\n--- SCAN RESULTS ---")
print(f"Total objects scanned in bucket: {total_objects}")
print(f"Total size scanned: {round(total_bytes_scanned / (1024 * 1024), 2)} MB")
print(f"Eligible objects (> {cutoff_days} days): {len(eligible_objects)}")
total_eligible_bytes = sum(o["size"] for o in eligible_objects)
print(f"Total eligible size: {round(total_eligible_bytes / (1024 * 1024), 2)} MB")

print("\n--- TOP 20 LARGEST ELIGIBLE FILES ---")
for idx, item in enumerate(eligible_objects[:20], 1):
    print(f"{idx:2d}. [{item['size_mb']:7.2f} MB] (Age: {item['age_days']} days, LastMod: {item['last_modified'][:10]}) -> {item['key']}")

