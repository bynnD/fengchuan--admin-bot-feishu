
import os
import sys

# Ensure current directory is in path
sys.path.append(os.getcwd())

try:
    from main import _fetch_approval_definition, APPROVAL_CODES
    
    def run():
        print("Starting definition fetch test...")
        code = APPROVAL_CODES.get("请假")
        if not code:
            print("Approval code not found for '请假'")
            return

        print(f"Fetching definition for code: {code}")
        res = _fetch_approval_definition(code)
        
        if res:
            print("Successfully fetched definition!")
            # It should be saved to last_approval_definition.json by main.py modification
        else:
            print("Failed to fetch definition.")

    if __name__ == "__main__":
        run()

except ImportError as e:
    print(f"Import failed: {e}")
except Exception as e:
    print(f"Error during execution: {e}")
