from fastapi import FastAPI

from app.routers import router

app = FastAPI(
    title="散货船滞期费结算服务",
    version="1.0.0",
    description=(
        "按统一边界规则计算散货船滞期费：作业区间左闭右开，"
        "暂停先裁剪再合并（重叠/首尾相接）得到净作业秒数，"
        "再扣除不超过净作业时长的租约允许秒数，余额不足一小时向上取整。"
    ),
)

app.include_router(router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
