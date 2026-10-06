import logging
from xts_agent.config_loader import AiRcaConfig
from xts_agent.rca.llm_provider import get_llm_provider
from xts_agent.rca.code_indexer import OEMCodeIndexer

logger = logging.getLogger(__name__)

class AITriageEngine:
    def __init__(self, config: AiRcaConfig):
        self.config = config
        self.provider = get_llm_provider(config)
        self.indexer = OEMCodeIndexer(config.index_db_path, config.source_code_paths)
        
    def triage_failure(self, test_name: str, stack_trace: str) -> str:
        """
        Analyzes a single test failure using RAG over the OEM codebase.
        """
        logger.info(f"Triggering AI Triage for {test_name}")
        
        # 1. Search OEM Code
        search_query = f"Crash in {test_name}\nStack trace:\n{stack_trace}"
        oem_context = self.indexer.search(search_query, top_k=3)
        
        # 2. Build Prompt
        prompt = f"""
You are an expert Android OS systems engineer. A CTS/VTS test failed.

Test Name: {test_name}

Failure Stack Trace:
```
{stack_trace}
```

Relevant OEM Source Code Retrieved:
```
{oem_context}
```

Task:
1. Identify the root cause of the crash or failure.
2. Determine if it is a mismatch in the OEM code provided above.
3. Provide a brief recommendation or code patch to fix it.

Format your response clearly.
"""

        # 3. Generate RCA
        logger.info(f"Generating AI RCA via {self.config.provider}...")
        rca_result = self.provider.generate(prompt)
        
        return rca_result
