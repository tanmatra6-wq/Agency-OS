import re

with open('control-plane/app/adapters/build_agent.py', 'r') as f:
    content = f.read()

# Add env parameter to subprocess calls
env_code = """
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
"""

if "GIT_CONFIG_GLOBAL" not in content:
    content = content.replace("def clone_and_checkout(self, create_new: bool = True) -> bool:", "def clone_and_checkout(self, create_new: bool = True) -> bool:\n" + env_code)
    # Also for merge_and_push and revert_last_merge
    content = content.replace("def merge_and_push(self, from_branch: str) -> bool:", "def merge_and_push(self, from_branch: str) -> bool:\n" + env_code)
    content = content.replace("def revert_last_merge(self) -> bool:", "def revert_last_merge(self) -> bool:\n" + env_code)
    
    # Now replace subprocess.run(..., cwd=...) with subprocess.run(..., env=env, cwd=...)
    content = re.sub(r'subprocess\.run\(\s*\[(.*?)\],\s*cwd=(.*?),', r'subprocess.run([\1], env=env, cwd=\2,', content)
    # also for without cwd:
    content = re.sub(r'subprocess\.run\(\s*\[(.*?)\],\s*check=True', r'subprocess.run([\1], env=env, check=True', content)
    
    with open('control-plane/app/adapters/build_agent.py', 'w') as f:
        f.write(content)
