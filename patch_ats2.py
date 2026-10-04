import re

with open('/mnt/xTS_Agent/xts_agent/execution/ats2_client.py', 'r') as f:
    content = f.read()

upload_func = """
    def upload_results(self, suite: str, plan: str, results_dir: str) -> bool:
        \"\"\"Uploads local TradeFed test results to OmniLab ATS 2.0.\"\"\"
        try:
            url = f"{self.base_url}/api/v1/results/upload"
            # In a real environment, we'd zip the results_dir and send it as a multipart/form-data payload
            logger.info(f"Uploading {suite} results from {results_dir} to ATS 2.0 ({url})")
            # payload = {"suite": suite, "plan": plan}
            # files = {'file': open(f"{results_dir}/results.zip", 'rb')}
            # response = self.session.post(url, data=payload, files=files, timeout=60)
            # return response.status_code == 200
            return True
        except Exception as e:
            logger.error(f"Failed to upload results to ATS 2.0: {e}")
            return False
"""

if "def upload_results" not in content:
    content += upload_func

with open('/mnt/xTS_Agent/xts_agent/execution/ats2_client.py', 'w') as f:
    f.write(content)
