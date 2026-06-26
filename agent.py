import os
from dotenv import load_dotenv

# LangChain Imports
from langchain_groq import ChatGroq
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate

# Your Tool Imports
from tools.mail_sender import send_email_report
from state import verification_state
from tools.log_analyzer import analyze_logs
from tools.rtl_analysis import analyze_rtl
from tools.rtl_parser import rtl_parser
from tools.simulation import run_simulation
from tools.tb_analysis import analyze_testbench
from tools.report import generate_report

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY not found in environment.")

def run_verification() -> str:
    """
    Run RTL verification using a LangChain Agent to dynamically analyze 
    design issues and missing testbench edge cases.
    """
    # Ensure files were initialized by rtl_verify.py
    if not verification_state.get("rtl_file"):
        raise ValueError("RTL file path not set in verification state.")
    if not verification_state.get("tb_file"):
        raise ValueError("Testbench file path not set in verification state.")

    # 1. Initialize the LLM via Groq
   
    llm = ChatGroq(
        temperature=0.2, 
        model_name="llama-3.3-70b-versatile", 
        groq_api_key=GROQ_API_KEY
    )

    # 2. Define the tools available to the agent
    tools = [
        rtl_parser, 
        run_simulation, 
        analyze_logs, 
        analyze_rtl, 
        analyze_testbench, 
        generate_report, 
        send_email_report
    ]

    
    system_prompt = """You are a Universal Hardware Verification Architect. 
    
    [CORE OBJECTIVE]
    Perform rigorous verification of any given RTL design. If you identify any HIGH or MEDIUM risk issues during RTL analysis, you MUST set the report status to 'FAIL' regardless of the simulation result.
    
    [STRICT SEQUENTIAL PROTOCOL]
    You MUST execute the following steps in this exact order:
    1. Parse: `rtl_parser`
    2. Simulate: `run_simulation`
    3. Analyze Logs: `analyze_logs`
    4. Analyze RTL: `analyze_rtl`
    5. Analyze Testbench: `analyze_testbench`
    6. Report: `generate_report` (With code fixes)
    7. Notify: `send_email_report`
    
    [CODE GENERATION GUIDELINES]
    - When you identify a bug in the RTL, output the corrected Verilog code block.
    - When you identify a missing test case, output the specific testbench Verilog code.
    - Pass these code blocks into `generate_report` as `rtl_code_fixes` and `tb_code_fixes`.
    - Do not generalize; provide the specific line-level changes required to fix the design.
    
    [ADAPTABILITY]
    - You are working on a generic hardware module. Analyze it based on its specific inputs and outputs found in `module_info`. Do not make assumptions about its function.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    #  Construct the Agent and Executor
    agent = create_tool_calling_agent(llm, tools, prompt)
    
    # verbose=True lets  watch the agent's "chain of thought" in the console
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    
    user_instruction = (
            "Execute the 7-step verification pipeline in strict order. "
            "1: Parse, 2: Simulate, 3: Log Analysis, 4: RTL Analysis, 5: TB Analysis, 6: Report, 7: Email. "
            "Use the findings from steps 4 and 5 to create precise code patches for step 6."
        )
    
    
    try:
        response = agent_executor.invoke({"input": user_instruction})
        
        # Check if the final report was saved to state by the tool
        final_report = verification_state.get("final_report")
        if final_report:
            return final_report
            
        return response["output"]
        
    except Exception as exc:
        raise RuntimeError(f"Agent execution failed: {exc}") from exc