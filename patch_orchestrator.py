import re

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'r') as f:
    content = f.read()

# Make sure shutil is imported
if "import shutil" not in content:
    content = "import shutil\n" + content

# Replace the generate_reports logic for ATS 2.0
old_block = """        # Trigger ATS 2.0 Upload if configured
        ats2 = ATS2Client("http://ats2.omnilab.local", "dummy_api_key")
        for suite_name, suite_res in results.suites_results.items():
            ats2.upload_results(suite_name, self.plan.name, suite_res.results_dir)"""

new_block = """        # Zipping official Google results for ATS 2.0
        ats2 = ATS2Client("http://ats2.omnilab.local", "dummy_api_key")
        for suite_name, suite_res in results.suites_results.items():
            if suite_res.results_dir and Path(suite_res.results_dir).exists():
                zip_path = Path(suite_res.results_dir).with_suffix('.zip')
                logger.info(f"Zipping official Google results: {suite_res.results_dir}")
                shutil.make_archive(str(zip_path.with_suffix('')), 'zip', suite_res.results_dir)
                logger.info(f"Uploading official compliance zip {zip_path} to ATS 2.0...")
                ats2.upload_results(suite_name, self.plan.name, str(zip_path))
            else:
                logger.warning(f"Results dir not found for suite {suite_name}, skipping ATS 2.0 upload.")"""

content = content.replace(old_block, new_block)

with open('/mnt/xTS_Agent/xts_agent/orchestrator.py', 'w') as f:
    f.write(content)

