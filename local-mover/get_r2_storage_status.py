import os
import boto3
from collections import defaultdict
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

print(f"Calculating current storage usage for Cloudflare R2 bucket '{bucket_name}'...")

paginator = s3.get_paginator("list_objects_v2")
page_iterator = paginator.paginate(Bucket=bucket_name)

total_objects = 0
total_size_bytes = 0
prefix_stats = defaultdict(lambda: {"count": 0, "bytes": 0})

for page in page_iterator:
    for obj in page.get("Contents", []):
        total_objects += 1
        size = obj["Size"]
        total_size_bytes += size
        
        # Extract top-level folder prefix
        key_parts = obj["Key"].split("/")
        prefix = key_parts[0] if len(key_parts) > 1 else "(root files)"
        prefix_stats[prefix]["count"] += 1
        prefix_stats[prefix]["bytes"] += size

total_size_mb = round(total_size_bytes / (1024 * 1024), 2)
total_size_gb = round(total_size_bytes / (1024 * 1024 * 1024), 3)

print("\n=======================================================")
print(f" CLOUDFLARE R2 BUCKET SUMMARY: '{bucket_name}'")
print("=======================================================")
print(f" Total Object Count: {total_objects:,} files")
print(f" Total Storage Used: {total_size_gb} GB ({total_size_mb:,} MB / {total_size_bytes:,} Bytes)")
print("-------------------------------------------------------")
print(" FOLDER / PREFIX BREAKDOWN:")
print("-------------------------------------------------------")

sorted_prefixes = sorted(prefix_stats.items(), key=lambda x: x[1]["bytes"], reverse=True)
for prefix, stat in sorted_prefixes:
    mb = round(stat["bytes"] / (1024 * 1024), 2)
    gb = round(stat["bytes"] / (1024 * 1024 * 1024), 2)
    pct = round((stat["bytes"] / total_size_bytes) * 100, 1) if total_size_bytes > 0 else 0
    print(f" 📂 {prefix:<32} {stat['count']:5d} files | {mb:8.2f} MB ({gb:4.2f} GB) | {pct:4.1f}%")

print("=======================================================\n")
