import asyncio
import os
from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService

from agent import root_agent

async def main():
    runner = Runner(
        app_name="test_app",
        agent=root_agent,
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService()
    )
    session = await runner.session_service.create_session(
        app_name="test_app", user_id="test_user"
    )
    
    print("Testing interaction...")
    
    # Send a topic
    content = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Generate a simple presentation about Python programming.")]
    )
    
    async for event in runner.run_async(user_id="test_user", session_id=session.id, new_message=content):
        if event.content and event.content.parts:
            print("AGENT REPLIES:", "".join(p.text for p in event.content.parts if p.text))
        if event.actions and hasattr(event.actions, 'function_calls') and event.actions.function_calls:
            print("FUNCTION CALL:", event.actions.function_calls)
        if event.actions and hasattr(event.actions, 'tool_calls') and event.actions.tool_calls:
            print("TOOL CALL:", event.actions.tool_calls)
            
if __name__ == "__main__":
    asyncio.run(main())
