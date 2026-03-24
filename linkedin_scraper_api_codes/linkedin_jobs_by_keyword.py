import argparse
import os
from dotenv import load_dotenv

import requests
import json
import time
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


class LinkedInJobsDiscovery:
    def __init__(self, api_token):
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self.dataset_id = "gd_lpfll7v5hcqtkxl6l"

    def discover_jobs(self, search_criteria, output="linkedin_jobs_keyword.json"):
        try:
            start_time = time.time()
            logging.info("Discovering jobs")

            trigger_response = self._trigger_collection(search_criteria)
            if not trigger_response or "snapshot_id" not in trigger_response:
                raise Exception("Failed to initiate job discovery")
            snapshot_id = trigger_response["snapshot_id"]
            jobs_data = None

            while True:
                status = self._check_status(snapshot_id)
                elapsed = int(time.time() - start_time)

                if status == "running":
                    logging.info(f"Status: {status} ({elapsed}s elapsed)")
                    time.sleep(5)
                    continue
                elif status == "ready":
                    if jobs_data is None:
                        jobs_data = self._get_data(snapshot_id)
                        if jobs_data:
                            logging.info(f"Discovery completed after {elapsed} seconds")
                            metadata = {
                                "Scraped By": "BrightData API - by keyword",
                                "Scraped Date": self._get_timestamp()
                            }
                            self._save_data(jobs_data, filename=output, metadata=metadata)
                            return jobs_data
                    break
                elif status in ["failed", "error"]:
                    raise Exception(f"Discovery failed with status: {status}")
                time.sleep(5)
        except Exception as e:
            logging.error(f"Error during job discovery: {str(e)}")
            return None

    def _trigger_collection(self, search_criteria):
        try:
            response = requests.post(
                "https://api.brightdata.com/datasets/v3/trigger",
                headers=self.headers,
                params={
                    "dataset_id": self.dataset_id,
                    "type": "discover_new",
                    "discover_by": "keyword",
                    "include_errors": "true",
                },
                json=search_criteria,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Error triggering discovery: {str(e)}")
            return None

    def _check_status(self, snapshot_id):
        try:
            response = requests.get(
                f"https://api.brightdata.com/datasets/v3/progress/{snapshot_id}",
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()
            return response.json().get("status")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error checking status: {str(e)}")
            return "error"

    def _get_data(self, snapshot_id):
        try:
            response = requests.get(
                f"https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}",
                headers=self.headers,
                params={"format": "json"},
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Error retrieving data: {str(e)}")
            return None

    def _normalize_records(self, data, metadata=None):
        if data is None:
            records = []
        elif isinstance(data, list):
            records = data
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            records = data["data"]
        elif isinstance(data, dict):
            records = [data]
        else:
            records = [{"value": data}]

        normalized = []
        for item in records:
            if isinstance(item, dict):
                enriched = item.copy()
                if metadata:
                    enriched["metadata"] = metadata
                normalized.append(enriched)
            else:
                wrapped = {"value": item}
                if metadata:
                    wrapped["metadata"] = metadata
                normalized.append(wrapped)
        return normalized

    def _parse_json_stream(self, raw_text):
        decoder = json.JSONDecoder()
        idx = 0
        length = len(raw_text)
        values = []

        while idx < length:
            while idx < length and raw_text[idx].isspace():
                idx += 1
            if idx >= length:
                break

            value, end = decoder.raw_decode(raw_text, idx)
            values.append(value)
            idx = end

            while idx < length and raw_text[idx].isspace():
                idx += 1
            if idx < length and raw_text[idx] == ',':
                idx += 1

        return values

    def _load_existing_records(self, filename):
        if not os.path.exists(filename):
            return []

        try:
            with open(filename, "r", encoding="utf-8") as f:
                raw_text = f.read().strip()
            if not raw_text:
                return []

            try:
                parsed = json.loads(raw_text)
                return self._normalize_records(parsed)
            except json.JSONDecodeError:
                parsed_values = self._parse_json_stream(raw_text)
                records = []
                for value in parsed_values:
                    records.extend(self._normalize_records(value))
                logging.warning(
                    f"Recovered {len(records)} existing records from concatenated JSON in {filename}"
                )
                return records
        except Exception as e:
            logging.error(f"Error reading existing data from {filename}: {str(e)}")
            return []

    def _save_data(self, data, filename="linkedin_jobs_keyword.json", metadata=None):
        try:
            existing_records = self._load_existing_records(filename)
            new_records = self._normalize_records(data, metadata=metadata)
            merged_records = existing_records + new_records

            with open(filename, "w", encoding="utf-8") as f:
                json.dump(merged_records, f, indent=2, ensure_ascii=False)

            logging.info(
                f"Saved {len(new_records)} new jobs to {filename} (total: {len(merged_records)})"
            )
        except Exception as e:
            logging.error(f"Error saving data: {str(e)}")

    def _get_timestamp(self):
        return datetime.now().strftime("%H:%M:%S")


def main():
    load_dotenv()
    api_token = os.getenv("BRIGHTDATA_APIKEY")
    discoverer = LinkedInJobsDiscovery(api_token)

    # CLI arguments (dynamic debugging)
    parser = argparse.ArgumentParser(description="Discover LinkedIn jobs by keyword")
    parser.add_argument("--location", default="New York", help="Location to search for jobs")
    parser.add_argument("--keyword", default="data analyst", help="Job keyword to search for")
    parser.add_argument("--country", default="US", help="Country to search for jobs")
    parser.add_argument("--time_range", default="Any time", help="Time range for job postings")
    parser.add_argument("--job_type", default="Part-time", help="Type of job (e.g., Full-time, Part-time)")
    parser.add_argument("--experience_level", default="Entry level", help="Experience level for job postings")
    parser.add_argument("--remote", default="Remote", help="Remote work option (e.g., Remote, On-site, Hybrid)")
    parser.add_argument("--company", default="", help="Company name to filter job postings")
    parser.add_argument("--output", default="linkedin_jobs_keyword.json", help="Output filename for discovered jobs")

    args = parser.parse_args()
    location = args.location
    keyword = args.keyword
    country = args.country
    time_range = args.time_range
    job_type = args.job_type
    exp_lvl = args.experience_level
    remote_option = args.remote
    company_name = args.company
    output_filepath = args.output

    search_criteria = [
        {
            "location": location,
            "keyword": keyword,
            "country": country,
            "time_range": time_range,
            "job_type": job_type,
            "experience_level": exp_lvl,
            "remote": remote_option,
            "company": company_name
        },
    ]

    discoverer.discover_jobs(search_criteria, output=output_filepath)


if __name__ == "__main__":
    main()
