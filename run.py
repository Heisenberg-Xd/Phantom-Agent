import subprocess
import sys

cmd = [
    sys.executable, 'main.py',
    '--url', 'https://demo.testfire.net',
    '--name', 'Altoro Bank',
    '--description', 'Demo banking app. Users login, view accounts, transfer money, view transactions, apply for loans, search, contact support.',
    '--login-steps', '[{"action": "goto", "url": "https://demo.testfire.net/login.jsp"}, {"action": "fill", "selector": "#uid", "value": "admin"}, {"action": "fill", "selector": "#passw", "value": "admin"}, {"action": "click", "selector": "[type=submit]"}, {"action": "wait", "ms": 2000}]'
]

print("Running command:", " ".join(cmd))
subprocess.run(cmd)
