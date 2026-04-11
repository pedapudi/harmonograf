import os
from google.adk.agents import Agent
from google.adk.tools import AgentTool
from google.adk.tools import FunctionTool

def write_webpage(topic: str, html_content: str, css_content: str, js_content: str) -> str:
    """Writes an interactive webpage files (HTML, CSS, JS) to the current directory."""
    try:
        topic_filename = topic.lower().replace(" ", "_").replace("/", "_")
        output_dir = os.path.join(os.path.dirname(__file__), "output", topic_filename)
        os.makedirs(output_dir, exist_ok=True)
        
        html_path = os.path.join(output_dir, "index.html")
        css_path = os.path.join(output_dir, "styles.css")
        js_path = os.path.join(output_dir, "script.js")
        
        with open(html_path, "w") as f:
            f.write(html_content)
        with open(css_path, "w") as f:
            f.write(css_content)
        with open(js_path, "w") as f:
            f.write(js_content)
            
        return f"Successfully created presentation on '{topic}' at {output_dir}"
    except Exception as e:
        return f"Error writing file: {e}"

write_webpage_tool = FunctionTool(write_webpage)

# Users can specify the model via environment variable. 
# For OpenAI compatible endpoints, they can provide a LiteLLM compliant string (e.g., 'openai/my-model')
# and set the `OPENAI_API_BASE` environment variable to their compatible endpoint.
MODEL_NAME = os.environ.get("USER_MODEL_NAME", "gemini-2.5-flash")

research_agent = Agent(
    name="research_agent",
    model=MODEL_NAME,
    instruction="""You are a researcher. Your goal is to gather information about the topic the user provides.
Think step-by-step and provide a comprehensive synthesis of high-quality bullet points and facts that can be used to generate a presentation slideshow.""",
    description="An agent capable of deeply reasoning and synthesizing a given topic for presentation notes.",
    tools=[] # Removed OpenAPI tool
)

web_developer_agent = Agent(
    name="web_developer_agent",
    model=MODEL_NAME,
    instruction="""You are an expert Frontend Web Developer. Your goal is to take research on a topic and generate a stunning, interactive, single-page presentation slideshow.
Generate beautiful semantic HTML structure, elegant CSS with modern design trends, animations, and transitions, and JavaScript for slideshow navigation (next/prev slides).
The HTML MUST include `<link rel="stylesheet" href="styles.css">` and `<script src="script.js"></script>` so the files are connected properly.
Remember to output the absolute final HTML, CSS, and JS using the `write_webpage` tool! Do not just print the code out, you must invoke the tool once everything is ready.""",
    description="An expert frontend developer agent that generates interactive HTML, CSS, and JS slideshow presentations and saves them to disk.",
    tools=[write_webpage_tool]
)

root_agent = Agent(
    name="coordinator_agent",
    model=MODEL_NAME,
    instruction="""You are the Coordinator Agent. Your task is to work with the user to pick a topic for an interactive slideshow presentation. 
First, get a topic from the user.
Second, transfer control to the 'research_agent' to gather comprehensive context and facts about the topic. Make sure to provide it with the topic!
Third, after researching, transfer control to the 'web_developer_agent' and provide it with all the researched materials. Instruct it to generate and save the presentation codebase.
Report back to the user when the task is complete.""",
    description="The main coordinator agent that drives the overall process of creating an interactive slideshow generation.",
    tools=[
        AgentTool(research_agent),
        AgentTool(web_developer_agent)
    ]
)
