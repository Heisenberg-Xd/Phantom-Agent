import asyncio
import os
import sys

os.environ["GEMINI_API_KEY"] = "AIzaSyA3k8_zBm7db9nAHy8y7r5eRYyWQcCHogg"

from agents.reporter import ReporterAgent

async def test():
    reporter = ReporterAgent()
    bug_reports = [
        {
            "title": "SQL Injection payload accepted in ToDo input field",
            "severity": "high",
            "steps_to_reproduce": [
                "Navigate to homepage",
                "Ensure URL is injected with ' OR 1=1"
            ],
            "expected_behavior": "Input escapes single quote.",
            "actual_behavior": "Executes query and dumps elements.",
            "affected_url": "http://todomvc.com/",
            "screenshot_path": ""
        }
    ]

    print("Running generate_fix_prompts...")
    
    def cb(msg):
        print(msg)
        
    prompts = await reporter.generate_fix_prompts(bug_reports, cb)
    print("Prompts generated:", prompts)

if __name__ == "__main__":
    asyncio.run(test())
