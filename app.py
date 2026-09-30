"""PocketSmart AI — FastAPI implementation based on the supplied project source."""
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from gemini_utils import API_KEY as GEMINI_API_KEY
from gemini_utils import (
    get_home_local_recommendations, get_home_recommendations,
    get_jewelry_local_recommendations, get_jewelry_recommendations,
    get_party_local_recommendations, get_party_recommendations,
)

load_dotenv()
logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="PocketSmart: AI Budget Planner", docs_url=None, redoc_url=None, openapi_url=None)
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
templates = Jinja2Templates(directory="templates")
os.makedirs("static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

# The source snippets use in-process dictionaries for users/sessions/history and show
# no database or persistence implementation. These values therefore last only until restart.
users_db: Dict[str, Dict[str, str]] = {}
active_sessions: Dict[str, Dict[str, Any]] = {}
blacklisted_tokens: set[str] = set()
user_recommendations: Dict[str, List[Dict[str, Any]]] = {}
_generation_tasks: Dict[str, asyncio.Task] = {}
_generation_tasks_lock = asyncio.Lock()


@app.exception_handler(RequestValidationError)
async def validation_error_as_bad_request(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"detail": "Invalid planner input. Check the submitted values."})


@app.get("/health")
async def health():
    return {"status": "ok"}


class HomeBudgetInput(BaseModel):
    total_budget: float
    num_lights: int = 0
    num_fans: int = 0
    num_furniture: int = 0
    num_dining_tables: int = 0
    has_living_room: bool = False
    has_kitchen: bool = False
    has_bedroom: bool = False
    additional_requirements: Optional[str] = None


class PartyBudgetInput(BaseModel):
    total_budget: float
    party_type: str
    num_guests: int
    venue_type: Optional[str] = None
    needs_catering: bool = True
    needs_decoration: bool = True
    needs_entertainment: bool = True
    additional_requirements: Optional[str] = None


class JewelryBudgetInput(BaseModel):
    total_budget: float
    occasion: str
    preferences: Optional[str] = None


class RegisterUser(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _make_token(username: str) -> str:
    expires = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expires}, SECRET_KEY, algorithm=ALGORITHM)


def _user_for_token(token: str) -> Optional[str]:
    if token in blacklisted_tokens:
        return None
    try:
        name = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        if name in users_db:
            return name
    except JWTError:
        return None
    return None


async def current_user(request: Request) -> Dict[str, str]:
    token = request.cookies.get("access_token")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:]
    username = _user_for_token(token or "")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    active_sessions[username]["last_activity"] = datetime.utcnow()
    return {"username": username, **users_db[username]}


def _page(request: Request, name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(name, {"request": request, **context})


@app.get("/", response_class=HTMLResponse)
async def home_page(request: Request):
    return _page(request, "index.html")


@app.get("/testimonials", response_class=HTMLResponse)
async def testimonials_page(request: Request):
    return _page(request, "testimonials.html")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "dashboard.html", user=user,
                 recommendations=user_recommendations.get(user["username"], [])[:3])


@app.get("/home-planner", response_class=HTMLResponse)
async def home_planner_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "home_planner.html", user=user)


@app.get("/party-planner", response_class=HTMLResponse)
async def party_planner_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "party_planner.html", user=user)


@app.get("/jewelry-planner", response_class=HTMLResponse)
async def jewelry_planner_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "jewelry_planner.html", user=user)


@app.get("/home-recommendations", response_class=HTMLResponse)
async def home_recommendations_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "home_recommendations.html", user=user, kind="home")


@app.get("/party-recommendations", response_class=HTMLResponse)
async def party_recommendations_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "party_recommendations.html", user=user, kind="party")


@app.get("/jewelry-recommendations", response_class=HTMLResponse)
async def jewelry_recommendations_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "jewelry_recommendations.html", user=user, kind="jewelry")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _page(request, "login.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _page(request, "register.html")


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, user: Dict[str, str] = Depends(current_user)):
    return _page(request, "history.html", user=user,
                 history=user_recommendations.get(user["username"], []))


def _record(username: str, kind: str, inputs: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    item = {"id": str(uuid4()), "timestamp": datetime.utcnow().isoformat(),
            "type": kind, "input": inputs, "result": result}
    user_recommendations.setdefault(username, []).insert(0, item)
    return item


async def _generate_and_record(username: str, kind: str, inputs: Dict[str, Any], generator, local_generator):
    key_material = json.dumps([username, kind, inputs], sort_keys=True, default=str, separators=(",", ":"))
    task_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    async with _generation_tasks_lock:
        task = _generation_tasks.get(task_key)
        if task is None:
            logger.info("[Planner] %s generation started", kind.title())
            task = asyncio.create_task(_perform_generation(username, kind, inputs, generator, local_generator))
            _generation_tasks[task_key] = task
            task.add_done_callback(
                lambda completed, key=task_key: asyncio.create_task(_forget_generation_task(key, completed))
            )
        else:
            logger.info("[Planner] Joined identical in-flight %s request", kind)
    return await asyncio.shield(task)


async def _forget_generation_task(task_key: str, task: asyncio.Task):
    async with _generation_tasks_lock:
        if _generation_tasks.get(task_key) is task:
            _generation_tasks.pop(task_key, None)


async def _perform_generation(username: str, kind: str, inputs: Dict[str, Any], generator, local_generator):
    try:
        result = await run_in_threadpool(generator)
    except HTTPException as exc:
        # Missing/invalid API key is a configuration problem, not a transient outage.
        if (exc.status_code == 503 and ("not configured" in str(exc.detail).lower() or "configuration failed" in str(exc.detail).lower() or not GEMINI_API_KEY)):
            logger.error("[Planner] Gemini API key configuration is missing")
            raise
        if exc.status_code not in (429, 500, 502, 503, 504):
            raise
        logger.warning("[Planner] Gemini %s generation failed with HTTP %s; using local fallback", kind, exc.status_code)
        result = local_generator()
    except Exception as exc:
        safe_message = str(exc)
        if GEMINI_API_KEY:
            safe_message = safe_message.replace(GEMINI_API_KEY, "[REDACTED]")
        logger.error("[Planner] Unexpected generation error: %s", safe_message[:500])
        result = local_generator()
    _record(username, kind, inputs, result)
    logger.info("[Planner] %s recommendation generated successfully (source=%s)", kind.title(), result.get("recommendation_source", "gemini"))
    return result


@app.post("/generate-home")
async def generate_home(data: HomeBudgetInput, user: Dict[str, str] = Depends(current_user)):
    if data.total_budget <= 0 or min(data.num_lights, data.num_fans, data.num_furniture, data.num_dining_tables) < 0:
        raise HTTPException(status_code=400, detail="Budget must be greater than zero and item counts cannot be negative.")
    logger.info(
        "[Planner] Home request accepted: authenticated=true total_budget=%s lights=%s fans=%s furniture=%s dining_tables=%s living_room=%s kitchen=%s bedroom=%s requirements_chars=%s",
        data.total_budget, data.num_lights, data.num_fans, data.num_furniture,
        data.num_dining_tables, data.has_living_room, data.has_kitchen,
        data.has_bedroom, len(data.additional_requirements or ""),
    )
    return await _generate_and_record(
        user["username"], "home", data.dict(),
        lambda: get_home_recommendations(data), lambda: get_home_local_recommendations(data),
    )


@app.post("/generate-party")
async def generate_party(data: PartyBudgetInput, user: Dict[str, str] = Depends(current_user)):
    if data.total_budget <= 0 or data.num_guests <= 0:
        raise HTTPException(status_code=400, detail="Budget and guest count must be greater than zero.")
    return await _generate_and_record(
        user["username"], "party", data.dict(),
        lambda: get_party_recommendations(data), lambda: get_party_local_recommendations(data),
    )


@app.post("/generate-jewelry")
async def generate_jewelry(
    total_budget: float = Form(...), occasion: str = Form(...),
    preferences: Optional[str] = Form(None), image: Optional[UploadFile] = File(None),
    user: Dict[str, str] = Depends(current_user)
):
    if total_budget <= 0:
        raise HTTPException(status_code=400, detail="Budget must be greater than zero.")
    data = JewelryBudgetInput(total_budget=total_budget, occasion=occasion, preferences=preferences)
    image_path = None
    if image:
        os.makedirs("static/uploads", exist_ok=True)
        image_path = os.path.join("static/uploads", os.path.basename(image.filename or "outfit-upload"))
        content = await image.read()
        with open(image_path, "wb") as output:
            output.write(content)
    inputs = data.dict()
    if image:
        inputs["image"] = image.filename
    return await _generate_and_record(
        user["username"], "jewelry", inputs,
        lambda: get_jewelry_recommendations(data, image_path),
        lambda: get_jewelry_local_recommendations(data, image_path),
    )


@app.post("/register")
async def register(user: RegisterUser):
    if user.username in users_db:
        raise HTTPException(status_code=400, detail="Username already registered")
    users_db[user.username] = {
        "email": user.email,
        "full_name": user.full_name or "",
        "hashed_password": pwd_context.hash(user.password),
    }
    user_recommendations[user.username] = []
    return {"message": "Registration successful"}


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = users_db.get(username)
    if not user or not pwd_context.verify(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token = _make_token(username)
    active_sessions[username] = {"username": username, "login_time": datetime.utcnow(),
                                 "last_activity": datetime.utcnow(), "token": token, "user_data": {}}
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie("access_token", token, httponly=True,
                        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax")
    return response


@app.post("/token", response_model=Token)
async def login_for_access_token(username: str = Form(...), password: str = Form(...)):
    user = users_db.get(username)
    if not user or not pwd_context.verify(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    token = _make_token(username)
    active_sessions[username] = {"username": username, "login_time": datetime.utcnow(),
                                 "last_activity": datetime.utcnow(), "token": token, "user_data": {}}
    return Token(access_token=token)


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("access_token")
    username = _user_for_token(token or "")
    if token:
        blacklisted_tokens.add(token)
    if username:
        active_sessions.pop(username, None)
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key="access_token")
    return response


@app.get("/session-info")
async def session_info(user: Dict[str, str] = Depends(current_user)):
    session = active_sessions.get(user["username"])
    if not session:
        raise HTTPException(status_code=404, detail="No active session found")
    return {"username": session["username"], "login_time": session["login_time"],
            "last_activity": session["last_activity"],
            "session_duration": (datetime.utcnow() - session["login_time"]).total_seconds() // 60,
            "user_data": session["user_data"]}


@app.post("/session-data")
async def update_session_data(data: Dict[str, Any], user: Dict[str, str] = Depends(current_user)):
    session = active_sessions.get(user["username"])
    if not session:
        raise HTTPException(status_code=404, detail="No active session found")
    session["user_data"].update(data)
    session["last_activity"] = datetime.utcnow()
    return {"message": "Session data updated", "data": session["user_data"]}


@app.get("/recommendations-details")
async def recommendations_details(recommendation_id: Optional[str] = None,
                                  user: Dict[str, str] = Depends(current_user)):
    items = user_recommendations.get(user["username"], [])
    if recommendation_id:
        for item in items:
            if item["id"] == recommendation_id:
                return item
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return {"recommendations": items}


async def cleanup_expired_sessions():
    while True:
        now = datetime.utcnow()
        expired = [name for name, session in active_sessions.items()
                   if (now - session["last_activity"]).total_seconds() > 1800]
        for name in expired:
            active_sessions.pop(name, None)
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup():
    logger.info("FastAPI startup complete; Gemini API key loaded=%s", bool(GEMINI_API_KEY))
    app.state.session_cleanup_task = asyncio.create_task(cleanup_expired_sessions())


@app.get("/startup")
async def startup_route():
    """The source names /startup but gives no separate request/response contract."""
    return {"status": "Application startup services are active"}


@app.on_event("shutdown")
async def stop_cleanup():
    task = getattr(app.state, "session_cleanup_task", None)
    if task:
        task.cancel()
