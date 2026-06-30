```python
# features/readme/test_readme_content.py

import pytest

def test_system_overview_in_readme():
    with open('README.md', 'r') as file:
        content = file.read()
        assert "System Overview" in content

def test_architecture_in_readme():
    with open('README.md', 'r') as file:
        content = file.read()
        assert "Architecture" in content

def test_core_principles_in_readme():
    with open('README.md', 'r') as file:
        content = file.read()
        assert "Core Principles" in content

def test_project_description_in_readme():
    with open('README.md', 'r') as file:
        content = file.read()
        assert "OpenClaw AI Software Engineering System" in content
        assert "Description of the system" in content
```