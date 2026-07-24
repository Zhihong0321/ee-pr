import os
import sys
import json
import datetime
import boto3
import requests
from dotenv import load_dotenv

load_dotenv()

r2_endpoint = os.getenv("R2_ENDPOINT")
r2_access_key = os.getenv("R2_ACCESS_KEY_ID")
r2_secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
bucket_name = os.getenv("R2_BUCKET", "eternalgy-image")
pcloud_base_url = os.getenv("PCLOUD_PUBLIC_BASE_URL", "https://filedn.com/loyekJXFL3Gh2dDJpskERa4")
pg_proxy_url = os.getenv("PG_PROXY_URL")
pg_proxy_token = os.getenv("PG_PROXY_AUTH_TOKEN")
pg_db_name = os.getenv("PG_PROXY_DB_NAME", "prod_main")

s3 = boto3.client(
    "s3",
    endpoint_url=r2_endpoint,
    aws_access_key_id=r2_access_key,
    aws_secret_access_key=r2_secret_key,
    region_name="auto",
)

def query_pg(sql, params=None):
    if not params:
        params = []
    resp = requests.post(
        f"{pg_proxy_url.rstrip('/')}/api/sql",
        headers={
            "Authorization": f"Bearer {pg_proxy_token}",
            "Content-Type": "application/json"
        },
        json={
            "db_name": pg_db_name,
            "sql": sql,
            "params": params
        },
        timeout=15
    )
    if resp.status_code == 200:
        return resp.json().get("rows", [])
    else:
        print(f"PG query error: {resp.status_code} - {resp.text}")
        return []

def generate_move_list(min_age_days=0, limit=500):
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff_date = now - datetime.timedelta(days=min_age_days)
    
    print(f"Scanning R2 bucket '{bucket_name}' for files (min_age_days={min_age_days}, cutoff={cutoff_date.strftime('%Y-%m-%d')})...")
    
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name)
    
    r2_objects = []
    for page in page_iterator:
        for obj in page.get("Contents", []):
            if obj["LastModified"] <= cutoff_date:
                r2_objects.append({
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "size_mb": round(obj["Size"] / (1024 * 1024), 2),
                    "last_modified": obj["LastModified"].isoformat(),
                    "age_days": (now - obj["LastModified"]).days,
                    "etag": obj.get("ETag", "").strip('"')
                })
                
    # Sort by size DESCENDING
    r2_objects.sort(key=lambda x: x["size"], reverse=True)
    
    print(f"Found {len(r2_objects)} objects matching age filter (>={min_age_days} days).")
    
    move_list = []
    for item in r2_objects[:limit]:
        pcloud_rel_path = f"R2-Archive/{item['key']}"
        pcloud_public_url = f"{pcloud_base_url.rstrip('/')}/{pcloud_rel_path}"
        r2_public_url = f"{os.getenv('R2_PUBLIC_BASE_URL').rstrip('/')}/{item['key']}"
        
        move_item = {
            "r2_key": item["key"],
            "size_bytes": item["size"],
            "size_mb": item["size_mb"],
            "age_days": item["age_days"],
            "last_modified": item["last_modified"],
            "r2_url": r2_public_url,
            "pcloud_path": pcloud_rel_path,
            "pcloud_public_url": pcloud_public_url,
            "db_match": None
        }
        move_list.append(move_item)
        
    return move_list

if __name__ == "__main__":
    age = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    move_list = generate_move_list(min_age_days=age, limit=100)
    
    output_file = "move_list_preview.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(move_list, f, indent=2)
        
    print(f"\nSaved preview of top {len(move_list)} largest files to '{output_file}'.")
