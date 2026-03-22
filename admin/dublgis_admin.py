from fastapi import APIRouter

router = APIRouter(prefix="/dublgis", tags=["dublgis"])


@router.get("/categories")
async def list_categories():
    return [222, 112852]
