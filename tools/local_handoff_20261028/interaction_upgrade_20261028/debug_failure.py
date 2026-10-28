import pytest,traceback
@pytest.fixture(autouse=True)
def trace_model_errors(monkeypatch):
 from malbut_agent_server.orchestrator import AgentOrchestrator
 original=AgentOrchestrator.handle
 def handle(*args,**kwargs):
  try:return original(*args,**kwargs)
  except BaseException:
   traceback.print_exc()
   raise
 monkeypatch.setattr(AgentOrchestrator,'handle',handle)
