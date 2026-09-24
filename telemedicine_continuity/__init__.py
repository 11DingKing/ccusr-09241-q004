"""远程诊疗链路连续性编排中心的服务端包入口。"""

PROJECT_CODE = "telemedicine_continuity"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识，供运行检查和诊断使用。"""
    return {"code": PROJECT_CODE, "title": "远程诊疗链路连续性编排中心"}
