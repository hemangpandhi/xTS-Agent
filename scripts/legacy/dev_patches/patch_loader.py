import re

with open('/mnt/xTS_Agent/xts_agent/config_loader.py', 'r') as f:
    content = f.read()

def filter_kwargs(cls_name, kwargs_str):
    if cls_name == "ShardingConfig":
        return f"ShardingConfig(shard_count=suite_data.get('sharding', {{}}).get('shard_count', 1))"
    if cls_name == "DeviceRequirements":
        return f"DeviceRequirements(min_devices=suite_data.get('device_requirements', {{}}).get('min_devices', 1))"
    return kwargs_str

content = re.sub(r'sharding=ShardingConfig\(\*\*suite_data\.get\([^)]+\)\)', "sharding=ShardingConfig(shard_count=suite_data.get('sharding', {}).get('shard_count', 1))", content)
content = re.sub(r'device_requirements=DeviceRequirements\(\*\*suite_data\.get\([^)]+\)\)', "device_requirements=DeviceRequirements(min_devices=suite_data.get('device_requirements', {}).get('min_devices', 1))", content)

with open('/mnt/xTS_Agent/xts_agent/config_loader.py', 'w') as f:
    f.write(content)
