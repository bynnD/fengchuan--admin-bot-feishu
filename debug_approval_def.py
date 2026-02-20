
import os
import json
import httpx
import time
from approval_config import APPROVAL_CODES

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

def get_token():
    res = httpx.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10
    )
    data = res.json()
    return data.get("tenant_access_token")

def fetch_definition(name, code):
    token = get_token()
    print(f"Fetching definition for {name} ({code})...")
    res = httpx.get(
        f"https://open.feishu.cn/open-apis/approval/v4/approvals/{code}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10
    )
    data = res.json()
    if data.get("code") != 0:
        print(f"Error: {data}")
        return

    payload = data.get("data", {})
    approval = payload.get("approval", payload)
    form = approval.get("form")
    if isinstance(form, str):
        form = json.loads(form)
    
    print(f"\n=== {name} Form Structure ===")
    # 递归打印 widget id, type, name, 和 children
    def print_widget(widgets, indent=0):
        for w in widgets:
            w_id = w.get("id")
            w_type = w.get("type")
            w_name = w.get("name") or w.get("label") or w.get("text")
            print(f"{'  ' * indent}- [{w_type}] {w_name} (ID: {w_id})")
            
            # 检查可能的子控件字段
            children = []
            for key in ["children", "sub_widgets", "widgets", "items", "fields"]:
                if key in w and isinstance(w[key], list):
                    children.extend(w[key])
            
            if children:
                print_widget(children, indent + 1)

    # 如果是列表直接打印，如果是字典则查找widgets
    if isinstance(form, list):
        print_widget(form)
    else:
        # 尝试查找通常包含控件列表的字段
        found = False
        for key in ["widgets", "children", "items"]:
            if key in form:
                print_widget(form[key])
                found = True
                break
        if not found:
            print(json.dumps(form, indent=2, ensure_ascii=False))

    # 特别打印 leaveGroupV2 / outGroup 的详细信息
    print(f"\n=== {name} Detailed Group Widgets ===")
    def find_group(widgets):
        for w in widgets:
            if w.get("type") in ["leaveGroupV2", "outGroup", "leaveGroup", "tripGroup"]:
                print(json.dumps(w, indent=2, ensure_ascii=False))
            
            # Recursion
            children = []
            for key in ["children", "sub_widgets", "widgets", "items", "fields"]:
                if key in w and isinstance(w[key], list):
                    children.extend(w[key])
            if children:
                find_group(children)
                
    if isinstance(form, list):
        find_group(form)
    elif isinstance(form, dict):
         for key in ["widgets", "children", "items"]:
            if key in form:
                find_group(form[key])

if __name__ == "__main__":
    fetch_definition("请假", APPROVAL_CODES["请假"])
    fetch_definition("外出", APPROVAL_CODES["外出"])
