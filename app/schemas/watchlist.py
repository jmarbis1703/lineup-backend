from pydantic import BaseModel


class WatchlistResponse(BaseModel):
    player_ids: list[int]


class WatchlistAddRequest(BaseModel):
    player_id: int
