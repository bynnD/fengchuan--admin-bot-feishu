# 行政审核规则配置文件
# 修改审核规则在这里调整，不需要动主程序

def validate_approval(approval_type, fields):
    """
    验证审批规则
    Returns: 
        status: "PASS" (通过), "WARN" (需注意), "BLOCK" (阻断)
        message: 审核意见或提示信息
    """
    
    if approval_type == "请假":
        try:
            days = float(fields.get("days", 0))
        except:
            days = 0
            
        if days <= 3:
            return "PASS", "行政审核：天数在规定范围内，符合规定。"
        elif days <= 7:
            return "WARN", "行政审核：请假超过3天，请确认假期余额充足。"
        else:
            return "WARN", "行政审核：请假超过7天，需总经理审批。"

    elif approval_type == "外出":
        return "PASS", "行政审核：外出申请符合规定。"

    elif approval_type == "用印申请":
        return "PASS", "行政审核：用印申请符合规定。"

    elif approval_type == "采购申请":
        try:
            budget_str = str(fields.get("budget", "0")).replace(",", "").replace("元", "")
            amount = float(budget_str)
            if amount <= 1000:
                return "PASS", "行政审核：金额1000元以内，符合规定。"
            elif amount <= 5000:
                return "WARN", "行政审核：金额在5000元以内，请附报价单。"
            else:
                return "WARN", "行政审核：金额超过5000元，需总经理审批确认。"
        except:
            return "BLOCK", "行政审核：请确认预算金额格式是否正确。"

    elif approval_type == "入职审批":
        return "PASS", "行政审核：入职信息已核实，符合规定。"

    # 默认规则
    return "PASS", "行政审核：符合规定。"

def get_admin_comment(approval_type, fields):
    """
    保持兼容性，用于生成表单中的行政意见
    """
    status, message = validate_approval(approval_type, fields)
    return message
