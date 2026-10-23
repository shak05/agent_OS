"""Demo agents. All are mock/offline; none contain scheduling, permission or recovery logic."""
from agents.docgen import DocGenAgent
from agents.email_agent import EmailAgent, MockMailTransport
from agents.filemanager import FileManagerAgent
from agents.research import ResearchAgent

__all__ = ["DocGenAgent", "EmailAgent", "FileManagerAgent", "MockMailTransport", "ResearchAgent"]
