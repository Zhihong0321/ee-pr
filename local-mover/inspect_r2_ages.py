import os
import datetime
import boto3
from dotenv import load_dotenv

load_dotenv()

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
    region_name="auto",
)

bucket_name = os.getenv("R2_BUCKET", "eternalgy-image")

paginator = s3.get_paginator("list_objects_v2")
page_iterator = paginator.paginate(Bucket=bucket_name)

all_objects = []
now = datetime.datetime.now(datetime.timezone.utc)

for page in page_iterator:
    for obj in page.get("Contents", []):
        size = obj["Size"]
        last_modified = obj["LastModified"]
        age_days = (now - last_modified).days
        all_objects.append({
            "key": obj["Key"],
            "size": size,
            "size_mb": round(size / (1024 * 1024), 2),
            "last_modified": last_modified.isoformat(),
            "age_days": age_days
        })

all_objects.sort(key=lambda x: x["size"], reverse=True)

print(f"Total objects: {len(all_objects)}")
min_age = min(o["age_days"] for o in all_objects) if all_objects else 0
max_age = max(o["age_days"] for o in all_objects) if all_objects else 0

print(f"Age range in bucket: Min {min_age} days old, Max {max_age} days old.")

print("\n--- AGE BREAKDOWN ---")
for threshold in [30, 60, 90, 120, 180]:
    count = sum(1 for o in all_objects if o["age_days"] >= threshold)
    total_size = sum(o["size"] for o in all_objects if o["age_days"] >= threshold)
    print(f"Older than {threshold:3d} days: {count:5d} objects ({round(total_size / (1024 * 1024), 2):8.2f} MB)")

print("\n--- TOP 10 LARGEST FILES OVERALL IN BUCKET ---")
for idx, item in enumerate(all_objects[:10], 1):
    print(f"{idx:2d}. [{item['size_mb']:7.2f} MB] (Age: {item['age_days']:3d} days, LastMod: {item['last_modified'][:10]}) -> {item['key']}")

