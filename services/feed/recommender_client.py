from typing import Optional

import httpx

from schemas import Event, RecommendationResponse, RecommendedItem


class HttpRecommenderClient:
    def __init__(self, base_url: str, timeout: float = 0.5) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def recommendations(self, user_id: str, cursor: Optional[str], limit: int) -> RecommendationResponse:
        params = {"user_id": user_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = await self._http.get("/recommendations", params=params)
        r.raise_for_status()
        return RecommendationResponse.model_validate(r.json())

    async def popular(self, limit: int) -> list[RecommendedItem]:
        r = await self._http.get("/popular", params={"limit": limit})
        r.raise_for_status()
        return [RecommendedItem.model_validate(x) for x in r.json()]

    async def close(self) -> None:
        await self._http.aclose()


class DirectEventPublisher:
    def __init__(self, base_url: str, timeout: float = 1.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def publish(self, event: Event) -> bool:
        try:
            r = await self._http.post("/events", content=event.model_dump_json(),
                                      headers={"Content-Type": "application/json"})
            r.raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._http.aclose()
