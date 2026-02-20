# 行政审核规则配置文件

def get_admin_comment(approval_type, fields):

    if approval_type == "请假":
        days = float(fields.get("days", 0))
        if days <= 3:
            return "行政审核：天数在规定范围内，同意。"
        elif days <= 7:
            return "行政审核：请假超过3天，请确认假期余额充足，同意。"
        else:
            return "行政审核：请假超过7天，需总经理审批，行政待确认。"

    elif approval_type == "外出":
        return "行政审核：外出申请已登记，同意。"

    elif approval_type == "用印申请":
        return "行政审核：用印申请已核实，同意。"

    elif approval_type == "采购申请":
        try:
            cost = str(fields.get("cost_detail", "0"))
            amount = float("".join(c for c in cost if c.isdigit() or c == ".") or "0")
            if amount <= 1000:
                return "行政审核：金额1000元以内，同意。"
            elif amount <= 5000:
                return "行政审核：金额在5000元以内，同意，请附报价单。"
            else:
                return "行政审核：金额超过5000元，需总经理审批确认。"
        except:
            return "行政审核：采购申请已收到，请确认费用明细。"

    elif approval_type == "入职审批":
        return "行政审核：入职信息已核实，同意，请HR跟进后续手续。"

    return "行政审核：已确认，同意。"
