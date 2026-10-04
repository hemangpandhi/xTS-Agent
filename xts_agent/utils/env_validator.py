import subprocess
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class EnvironmentValidator:
    @staticmethod
    def validate_aapt2(tradefed_script_path: Path):
        """Ensures the TradeFed script is using a valid, working AAPT2 binary."""
        logger.info("Running pre-flight check on AAPT2 parser...")
        
        # Check if the script contains the dangerous dynamic lookup
        with open(tradefed_script_path, 'r') as f:
            content = f.read()
            
        if "$(type -P aapt2" in content or "/usr/bin/aapt2" in content:
            logger.warning("Detected broken system AAPT2 mapping in TradeFed script!")
            
            # Find the newest Android SDK build-tools AAPT2
            sdk_path = Path("/home/hemang/Android/Sdk/build-tools")
            if sdk_path.exists():
                versions = sorted([d for d in sdk_path.iterdir() if d.is_dir()], reverse=True)
                if versions:
                    best_aapt2 = versions[0] / "aapt2"
                    if best_aapt2.exists():
                        logger.info(f"Auto-repairing TradeFed script with valid AAPT2: {best_aapt2}")
                        # Auto-patch the file
                        import re
                        new_content = re.sub(r'--aapt=.*?\\', f'--aapt={best_aapt2} \\\\', content)
                        with open(tradefed_script_path, 'w') as f:
                            f.write(new_content)
                        return True
            logger.error("Could not find a valid Android SDK AAPT2 to patch TradeFed!")
            return False
            
        logger.info("AAPT2 environment is healthy.")
        return True
