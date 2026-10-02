"""Gemini recommendation helpers based on the prompts visible in source screenshots."""
import json
import logging
import os
import re
import time
from importlib.metadata import version
from typing import Optional
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import HTTPException
from PIL import Image
from google import genai
from google.genai import errors, types

load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
logger = logging.getLogger("uvicorn.error")
GEMINI_REQUEST_TIMEOUT_SECONDS = 25
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.6-flash")
GEMINI_SDK_VERSION = version("google-genai")


def _client():
    if not API_KEY:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured in the backend environment.")
    timeout_ms = GEMINI_REQUEST_TIMEOUT_SECONDS * 1000
    return genai.Client(
        api_key=API_KEY,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(
                attempts=1,
                initial_delay=1,
                max_delay=2,
            ),
        ),
    )


def extract_json_from_response(text: str) -> dict:
    """Parse the JSON response requested by the source prompts."""
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end < start:
        raise HTTPException(status_code=502, detail="Gemini response did not contain JSON")
    try:
        return json.loads(clean[start:end + 1])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Gemini returned invalid JSON: {exc.msg}") from exc


def _local_item(name: str, description: str, price: float, quantity: int, terms: str) -> dict:
    return {
        "name": name,
        "description": description,
        "estimated_price": price,
        "quantity": quantity,
        "search_terms": terms,
    }


def get_home_local_recommendations(data) -> dict:
    """Generate a transparent, deterministic local plan when Gemini is unavailable."""
    categories = []
    requested = [
        ("Lighting", data.num_lights, 0.12, "modern LED light India"),
        ("Ceiling Fans", data.num_fans, 0.26, "energy efficient ceiling fan India"),
        ("Furniture", data.num_furniture, 0.38, "functional modern furniture India"),
        ("Dining Tables", data.num_dining_tables, 0.24, "affordable dining table India"),
    ]
    allocation_total = sum(weight for _, count, weight, _ in requested if count > 0)
    allocated = 0.0
    for index, (category, count, weight, terms) in enumerate(requested):
        if count <= 0:
            continue
        allocation = round(data.total_budget * weight / allocation_total, 2)
        if index == len(requested) - 1:
            allocation = round(data.total_budget - allocated, 2)
        allocated += allocation
        unit_price = round(allocation / count, 2)
        categories.append({
            "category": category,
            "allocation": allocation,
            "items": [_local_item(
                f"Budget-conscious {category.lower()} recommendation",
                "A practical, functional option selected to fit the requested budget. Compare current local prices, dimensions, energy ratings, warranty, and reviews before purchase.",
                unit_price, count, terms,
            )],
        })
    if not categories:
        categories.append({
            "category": "Living Room Essentials",
            "allocation": round(data.total_budget, 2),
            "items": [_local_item(
                "Flexible living room essentials budget",
                "Reserve this amount for functional essentials; compare size, warranty, and current local prices before purchase.",
                round(data.total_budget, 2), 1, "modern living room essentials India",
            )],
        })
    return {
        "total_budget": data.total_budget,
        "budget_breakdown": categories,
        "calculation_table": [
            {"category": c["category"], "items_count": sum(i["quantity"] for i in c["items"]),
             "total_cost": c["allocation"], "percentage_of_budget": round(c["allocation"] * 100 / data.total_budget, 2) if data.total_budget else 0}
            for c in categories
        ],
        "remaining_budget": round(data.total_budget - sum(c["allocation"] for c in categories), 2),
        "additional_suggestions": [
            "This is a locally generated fallback plan because Gemini could not be reached.",
            f"Rooms selected: {', '.join(name for name, enabled in [('Living Room', data.has_living_room), ('Kitchen', data.has_kitchen), ('Bedroom', data.has_bedroom)] if enabled) or 'None specified'}.",
            data.additional_requirements or "Compare current prices and verify dimensions before purchasing.",
        ],
        "recommendation_source": "local_fallback",
    }


def get_party_local_recommendations(data) -> dict:
    budget = data.total_budget
    breakdown = []
    for category, enabled, share, terms in [
        ("Venue", True, 0.35, "affordable event venue India"),
        ("Catering", data.needs_catering, 0.35, "budget catering per person India"),
        ("Decoration", data.needs_decoration, 0.18, "simple party decoration India"),
        ("Entertainment", data.needs_entertainment, 0.12, "affordable party entertainment India"),
    ]:
        if enabled:
            allocation = round(budget * share, 2)
            breakdown.append({"category": category.lower(), "allocation": allocation, "items": [_local_item(
                f"Budget-conscious {category.lower()} option", "Use this as a planning allowance and confirm current local availability and pricing.", allocation, 1, terms
            )]})
    return {"total_budget": budget, "budget_breakdown": breakdown,
            "venue_suggestions": [], "remaining_budget": round(budget - sum(c["allocation"] for c in breakdown), 2),
            "additional_suggestions": ["Locally generated fallback plan; confirm vendor quotes before booking.", f"Plan for {data.num_guests} guests."] ,
            "recommendation_source": "local_fallback"}


def get_jewelry_local_recommendations(data, image_path=None) -> dict:
    budget = data.total_budget
    return {"total_budget": budget, "jewelry_recommendations": [{
        "item_type": "Versatile everyday set", "description": "Compare lightweight, nickel-safe styles from established local sellers and verify return policies.",
        "style": data.preferences or "Simple contemporary", "estimated_price": round(budget * 0.8, 2),
        "search_terms": "affordable contemporary jewelry India",
    }], "remaining_budget": round(budget * 0.2, 2),
        "styling_tips": [f"Choose pieces suited to {data.occasion}.", "Locally generated fallback; verify material and seller details."],
        "recommendation_source": "local_fallback"}


def _generate(prompt: str, image=None) -> dict:
    started = time.monotonic()
    logger.info("[Gemini] Trying %s (SDK %s, API key loaded=%s)", GEMINI_MODEL, GEMINI_SDK_VERSION, bool(API_KEY))
    client = _client()
    try:
        contents = [prompt, image] if image is not None else prompt
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        )
        response_model = GEMINI_MODEL
        for fallback_model in GEMINI_FALLBACK_MODELS:
            try:
                response = client.models.generate_content(model=response_model, contents=contents, config=config)
                break
            except errors.APIError as exc:
                if exc.code not in (404, 429, 500, 502, 503, 504):
                    raise
                raw_message = str(exc)
                safe_message = raw_message.replace(API_KEY, "[REDACTED]") if API_KEY else raw_message
                logger.warning(
                    "[Gemini] Received HTTP %s from %s (%s); trying model %s",
                    exc.code, response_model, safe_message, fallback_model,
                )
                response_model = fallback_model
        else:
            try:
                response = client.models.generate_content(model=response_model, contents=contents, config=config)
            except Exception as exc:
                elapsed = time.monotonic() - started
                safe_message = str(exc).replace(API_KEY, "[REDACTED]") if API_KEY else str(exc)
                logger.error("[Gemini] All configured models unavailable after %.1f seconds: %s", elapsed, safe_message)
                raise HTTPException(status_code=502, detail=f"All Gemini models failed: {safe_message}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        elapsed = time.monotonic() - started
        raw_message = str(exc)
        safe_message = raw_message.replace(API_KEY, "[REDACTED]") if API_KEY else raw_message
        status_code = (
            exc.code if isinstance(exc, errors.APIError)
            else getattr(exc, "status_code", None)
        )
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            http_status, detail = 504, f"Gemini request timed out after {GEMINI_REQUEST_TIMEOUT_SECONDS} seconds."
        elif status_code in (401, 403):
            http_status, detail = 503, f"Gemini API authentication/configuration failed (HTTP {status_code}). Check GEMINI_API_KEY permissions."
        elif status_code == 404:
            http_status, detail = 502, f"Gemini model or endpoint not found (HTTP 404): {safe_message}"
        elif status_code == 429:
            http_status, detail = 429, f"Gemini API rate limit or quota exceeded (HTTP 429): {safe_message}"
        elif status_code == 503:
            http_status, detail = 503, f"Gemini API is temporarily unavailable (HTTP 503): {safe_message}"
        else:
            http_status, detail = 502, f"Gemini API error{f' (HTTP {status_code})' if status_code else ''}: {safe_message}"
        logger.error("Gemini request failed after %.1f seconds (%s): %s", elapsed, type(exc).__name__, safe_message)
        if status_code in (404, 429, 500, 502, 503, 504) or isinstance(exc, (TimeoutError, OSError)):
            logger.error("[Gemini] All models unavailable or unreachable")
        raise HTTPException(status_code=http_status, detail=detail) from exc
    finally:
        client.close()
    elapsed = time.monotonic() - started
    response_text = response.text or ""
    logger.info("Gemini response received from model=%s after %.1f seconds; text_length=%d", response_model, elapsed, len(response_text))
    if not response_text.strip():
        logger.error("Gemini returned no usable text after %.1f seconds", elapsed)
        raise HTTPException(status_code=502, detail="Gemini returned an empty response. Please retry.")
    try:
        result = extract_json_from_response(response_text)
    except HTTPException:
        logger.exception("Gemini response parsing failed after %.1f seconds", elapsed)
        raise
    logger.info("Gemini response parsed successfully after %.1f seconds", elapsed)
    return result


def get_home_recommendations(budget_input) -> dict:
    prompt = f"""
I need interior design product recommendations for a home in India with a total budget of ₹{budget_input.total_budget:.2f}.
Requirements:
- {budget_input.num_lights} lights/lighting fixtures
- {budget_input.num_fans} ceiling fans
- {budget_input.num_furniture} furniture pieces
- {budget_input.num_dining_tables} dining tables

Additional rooms to consider:
{('- Living room' if budget_input.has_living_room else '')}
{('- Kitchen' if budget_input.has_kitchen else '')}
{('- Bedroom' if budget_input.has_bedroom else '')}

Additional requirements: {budget_input.additional_requirements or 'None'}

Please provide a detailed budget breakdown with product recommendations available in India.
Use Indian brands and pricing. Include search terms suitable for Indian shopping platforms.
Format your response as JSON with the following structure:
{{
  "total_budget": {budget_input.total_budget:.2f},
  "budget_breakdown": [{{"category": "lighting", "allocation": 0.0, "items": [{{"name": "", "description": "", "estimated_price": 0.0, "quantity": 0, "search_terms": ""}}]}}],
  "calculation_table": [{{"category": "", "items_count": 0, "total_cost": 0.0, "percentage_of_budget": 0.0}}],
  "remaining_budget": 0.0,
  "additional_suggestions": []
}}
Ensure total costs stay within budget. Include search terms for each item to find on shopping websites like Flipkart, Amazon India, IKEA.
"""
    result = _generate(prompt)
    for category in result.get("budget_breakdown", []):
        for item in category.get("items", []):
            terms = item.get("search_terms", "")
            if terms:
                item["shopping_links"] = {
                    "amazon": f"https://www.amazon.in/s?k={quote_plus(terms)}",
                    "flipkart": f"https://www.flipkart.com/search?q={quote_plus(terms)}",
                    "ikea": f"https://www.ikea.com/in/en/search/?q={quote_plus(terms)}",
                    "myntra": f"https://www.myntra.com/search?q={quote_plus(terms)}",
                    "ajio": f"https://www.ajio.com/search/?text={quote_plus(terms)}",
                }
    return result


def get_party_recommendations(budget_input) -> dict:
    prompt = f"""
I need party planning recommendations for India with a total budget of ₹{budget_input.total_budget:.2f}.
Party details:
- Type: {budget_input.party_type}
- Number of guests: {budget_input.num_guests}
- Venue type: {budget_input.venue_type or 'Not specified'}
- Catering needed: {'Yes' if budget_input.needs_catering else 'No'}
- Decoration needed: {'Yes' if budget_input.needs_decoration else 'No'}
- Entertainment needed: {'Yes' if budget_input.needs_entertainment else 'No'}
Additional requirements: {budget_input.additional_requirements or 'None'}
Please provide a detailed budget breakdown with specific recommendations available in India using INR prices.
Use Indian brands, services, and typical cost expectations.
Format your response as JSON with total_budget, budget_breakdown (categories with allocation and items containing name, description, estimated_price, quantity, search_terms), venue_suggestions (name, type, capacity, estimated_cost, search_terms), remaining_budget, and additional_suggestions.
Ensure all costs are in INR and total does not exceed the given budget. Provide search terms suitable for Indian websites such as BookMyShow, Swiggy, Flipkart, etc.
"""
    result = _generate(prompt)
    platforms = {
        "venue": ["google", "booking", "makemytrip", "oyorooms", "nobroker"],
        "catering": ["swiggy", "zomato"],
        "food": ["swiggy", "zomato", "bigbasket", "amazon", "flipkart"],
        "drinks": ["swiggy", "zomato", "bigbasket", "amazon", "flipkart"],
        "decoration": ["amazon", "flipkart", "meesho", "myntra"],
        "entertainment": ["bookmyshow", "amazon", "flipkart"],
        "gifts": ["amazon", "flipkart", "myntra", "meesho"],
        "photography": ["google", "amazon", "flipkart"],
        "music": ["amazon", "flipkart", "bookmyshow"],
        "games": ["amazon", "flipkart"],
        "accessories": ["amazon", "flipkart", "myntra", "meesho"],
        "transportation": ["makemytrip", "google"],
        "return_gifts": ["amazon", "flipkart", "myntra", "meesho"],
    }
    urls = {
        "amazon": "https://www.amazon.in/s?k={}", "flipkart": "https://www.flipkart.com/search?q={}",
        "bigbasket": "https://www.bigbasket.com/ps/?q={}", "swiggy": "https://www.swiggy.com/search?query={}",
        "zomato": "https://www.zomato.com/search?q={}", "bookmyshow": "https://in.bookmyshow.com/search?q={}",
        "myntra": "https://www.myntra.com/search?q={}", "meesho": "https://www.meesho.com/search?q={}",
        "google": "https://www.google.com/search?q={}", "booking": "https://www.booking.com/search.html?ss={}",
        "makemytrip": "https://www.makemytrip.com/hotels/hotel-listing/?searchText={}",
        "oyorooms": "https://www.oyorooms.com/search/?location={}",
        "nobroker": "https://www.nobroker.in/property/search?searchTerm={}",
    }
    for category in result.get("budget_breakdown", []):
        names = platforms.get(category.get("category", "").lower(), ["amazon", "flipkart", "google"])
        for item in category.get("items", []):
            terms = item.get("search_terms", "")
            if terms:
                item["shopping_links"] = {key: urls[key].format(quote_plus(terms)) for key in names if key in urls}
    for venue in result.get("venue_suggestions", []):
        terms = venue.get("search_terms", "")
        if terms:
            venue["search_links"] = {key: urls[key].format(quote_plus(terms)) for key in platforms["venue"]}
    return result


def get_jewelry_recommendations(budget_input, image_path: Optional[str] = None) -> dict:
    base = f"""I need jewelry recommendations for India with a total budget of ₹{budget_input.total_budget:.2f}.
Occasion: {budget_input.occasion}
Preferences: {budget_input.preferences or 'Not specified'}
Provide only India-relevant styles, availability, and price ranges in INR.
"""
    image = Image.open(image_path) if image_path else None
    if image is not None:
        prompt = base + """
An image of the outfit is uploaded. Suggest jewelry that complements it, considering color, design, and occasion appropriateness.
Format the output as JSON with outfit_analysis (colors, style, formality), total_budget,
jewelry_recommendations (item_type, description, style, estimated_price, search_terms),
remaining_budget, and styling_tips. Make sure prices are in INR and stay within budget.
Include Indian-friendly search terms for shopping.
"""
    else:
        prompt = base + """
Format the output as JSON with total_budget, jewelry_recommendations
(item_type, description, style, estimated_price, search_terms), remaining_budget, and styling_tips.
Keep prices in INR and relevant to Indian brands.
"""
    result = _generate(prompt, image)
    for item in result.get("jewelry_recommendations", []):
        terms = item.get("search_terms", "")
        if terms:
            item["shopping_links"] = {
                "amazon": f"https://www.amazon.in/s?k={quote_plus(terms)}",
                "flipkart": f"https://www.flipkart.com/search?q={quote_plus(terms)}",
                "bluestone": f"https://www.bluestone.com/search.html?query={quote_plus(terms)}",
                "tanishq": f"https://www.tanishq.co.in/search/?q={quote_plus(terms)}",
                "caratlane": f"https://www.caratlane.com/search?q={quote_plus(terms)}",
                "melorra": f"https://www.melorra.com/search?q={quote_plus(terms)}",
                "meesho": f"https://www.meesho.com/search?q={quote_plus(terms)}",
            }
    return result
