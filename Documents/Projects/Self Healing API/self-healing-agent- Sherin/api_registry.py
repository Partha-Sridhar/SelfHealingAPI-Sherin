"""
API Registry — 18 Real-World APIs across 15 Domains
=====================================================

Each API has:
  - Live endpoint + params (V1 schema — what the LLM is told)
  - Drifted params (V2 schema — simulated API upgrade)
  - Docs URL (for schema discovery fallback)
  - OpenAPI URL if available

Schema discovery priority:
  1. OpenAPI/Swagger endpoint (instant, free)
  2. LLM doc scraping (1-2s, cached after first call)
"""

API_REGISTRY = {
    # ── Weather ──
    "get_weather": {
        "domain": "Weather",
        "provider": "Open-Meteo",
        "description": "Get current weather for a location by coordinates or city name.",
        "base_url": "https://api.open-meteo.com/v1/forecast",
        "docs_url": "https://open-meteo.com/en/docs",
        "openapi_url": None,
        "v1_schema": {
            "city": {"type": "string", "description": "City name", "required": True},
        },
        "v2_schema": {
            "location_name": {"type": "string", "description": "Name of the city or location", "required": True},
        },
        "sample_queries": [
            "What's the weather in London?",
            "Temperature in Tokyo right now",
            "Weather in Mumbai",
        ],
    },

    # ── Geography ──
    "get_country_info": {
        "domain": "Geography",
        "provider": "REST Countries",
        "description": "Get facts about a country including capital, population, region.",
        "base_url": "https://restcountries.com/v3.1/name/{country}",
        "docs_url": "https://restcountries.com/",
        "openapi_url": None,
        "v1_schema": {
            "country": {"type": "string", "description": "Country name", "required": True},
        },
        "v2_schema": {
            "country_name": {"type": "string", "description": "Full name of the country", "required": True},
        },
        "sample_queries": [
            "Tell me about Japan",
            "Capital of France",
            "Population of India",
        ],
    },

    # ── Crypto ──
    "get_crypto_price": {
        "domain": "Crypto",
        "provider": "CoinGecko",
        "description": "Get current cryptocurrency price in a target currency.",
        "base_url": "https://api.coingecko.com/api/v3/simple/price",
        "docs_url": "https://docs.coingecko.com/reference/simple-price",
        "openapi_url": None,
        "v1_schema": {
            "coin": {"type": "string", "description": "Cryptocurrency ID, e.g. bitcoin", "required": True},
            "currency": {"type": "string", "description": "Target currency, e.g. usd", "required": True},
        },
        "v2_schema": {
            "crypto_id": {"type": "string", "description": "Cryptocurrency identifier", "required": True},
            "vs_currency": {"type": "string", "description": "Fiat currency to compare against", "required": True},
        },
        "sample_queries": [
            "Bitcoin price in USD",
            "Ethereum price in EUR",
            "What's the price of solana?",
        ],
    },

    # ── Finance ──
    "get_exchange_rate": {
        "domain": "Finance",
        "provider": "Exchange Rate API",
        "description": "Get exchange rate between two currencies.",
        "base_url": "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base}.json",
        "docs_url": "https://github.com/fawazahmed0/exchange-api",
        "openapi_url": None,
        "v1_schema": {
            "base": {"type": "string", "description": "Base currency code, e.g. USD", "required": True},
            "target": {"type": "string", "description": "Target currency code, e.g. EUR", "required": True},
        },
        "v2_schema": {
            "from_currency": {"type": "string", "description": "Source currency code", "required": True},
            "to_currency": {"type": "string", "description": "Destination currency code", "required": True},
        },
        "sample_queries": [
            "Convert USD to EUR",
            "Exchange rate GBP to JPY",
            "How much is 1 INR in USD?",
        ],
    },

    # ── Books ──
    "search_books": {
        "domain": "Books",
        "provider": "Open Library",
        "description": "Search for books by title, author, or subject.",
        "base_url": "https://openlibrary.org/search.json",
        "docs_url": "https://openlibrary.org/developers/api",
        "openapi_url": None,
        "v1_schema": {
            "query": {"type": "string", "description": "Search query — title, author, or keyword", "required": True},
            "limit": {"type": "integer", "description": "Max results to return", "required": False},
        },
        "v2_schema": {
            "search_term": {"type": "string", "description": "Text to search for in books database", "required": True},
            "max_results": {"type": "integer", "description": "Maximum number of results", "required": False},
        },
        "sample_queries": [
            "Search for books about Python",
            "Find books by George Orwell",
            "Books about machine learning",
        ],
    },

    # ── Entertainment ──
    "get_joke": {
        "domain": "Entertainment",
        "provider": "JokeAPI",
        "description": "Get a random joke, optionally filtered by category.",
        "base_url": "https://v2.jokeapi.dev/joke/{category}",
        "docs_url": "https://v2.jokeapi.dev/",
        "openapi_url": None,
        "v1_schema": {
            "category": {"type": "string", "description": "Joke category: Programming, Misc, Pun, Spooky, Christmas, Any", "required": True},
        },
        "v2_schema": {
            "joke_type": {"type": "string", "description": "Category of joke to retrieve", "required": True},
        },
        "sample_queries": [
            "Tell me a programming joke",
            "Give me a random joke",
            "Tell me a pun",
        ],
    },

    # ── Education (Dictionary) ──
    "define_word": {
        "domain": "Education",
        "provider": "Free Dictionary API",
        "description": "Get the definition, phonetics, and examples for a word.",
        "base_url": "https://api.dictionaryapi.dev/api/v2/entries/en/{word}",
        "docs_url": "https://dictionaryapi.dev/",
        "openapi_url": None,
        "v1_schema": {
            "word": {"type": "string", "description": "The word to look up", "required": True},
        },
        "v2_schema": {
            "term": {"type": "string", "description": "Dictionary term to define", "required": True},
        },
        "sample_queries": [
            "Define the word 'serendipity'",
            "What does 'ephemeral' mean?",
            "Look up the word 'algorithm'",
        ],
    },

    # ── Education (Universities) ──
    "search_universities": {
        "domain": "Education",
        "provider": "Hipolabs Universities",
        "description": "Search for universities by name and country.",
        "base_url": "http://universities.hipolabs.com/search",
        "docs_url": "https://github.com/Hipo/university-domains-list-api",
        "openapi_url": None,
        "v1_schema": {
            "name": {"type": "string", "description": "University name to search", "required": True},
            "country": {"type": "string", "description": "Country to filter by", "required": False},
        },
        "v2_schema": {
            "university_name": {"type": "string", "description": "Name of the university", "required": True},
            "country_name": {"type": "string", "description": "Country where university is located", "required": False},
        },
        "sample_queries": [
            "Search for MIT",
            "Find universities in India",
            "Search for Oxford university",
        ],
    },

    # ── Food & Drink ──
    "search_cocktail": {
        "domain": "Food & Drink",
        "provider": "TheCocktailDB",
        "description": "Search for cocktail recipes by name.",
        "base_url": "https://www.thecocktaildb.com/api/json/v1/1/search.php",
        "docs_url": "https://www.thecocktaildb.com/api.php",
        "openapi_url": None,
        "v1_schema": {
            "name": {"type": "string", "description": "Cocktail name to search for", "required": True},
        },
        "v2_schema": {
            "drink_name": {"type": "string", "description": "Name of the cocktail or drink", "required": True},
        },
        "sample_queries": [
            "How to make a Margarita?",
            "Search for Mojito recipe",
            "What's in a Cosmopolitan?",
        ],
    },

    # ── Trivia ──
    "get_trivia": {
        "domain": "Trivia",
        "provider": "Open Trivia DB",
        "description": "Get trivia questions by category and difficulty.",
        "base_url": "https://opentdb.com/api.php",
        "docs_url": "https://opentdb.com/api_config.php",
        "openapi_url": None,
        "v1_schema": {
            "category": {"type": "integer", "description": "Category ID (9=General, 18=Computers, 21=Sports, 23=History)", "required": True},
            "difficulty": {"type": "string", "description": "easy, medium, or hard", "required": False},
        },
        "v2_schema": {
            "topic_id": {"type": "integer", "description": "Trivia topic category number", "required": True},
            "level": {"type": "string", "description": "Difficulty level of questions", "required": False},
        },
        "sample_queries": [
            "Give me a computer science trivia question",
            "Easy sports trivia",
            "Hard history trivia question",
        ],
    },

    # ── Gaming ──
    "get_pokemon": {
        "domain": "Gaming",
        "provider": "PokeAPI",
        "description": "Get details about a Pokemon by name or ID.",
        "base_url": "https://pokeapi.co/api/v2/pokemon/{name}",
        "docs_url": "https://pokeapi.co/docs/v2",
        "openapi_url": None,
        "v1_schema": {
            "name": {"type": "string", "description": "Pokemon name or ID", "required": True},
        },
        "v2_schema": {
            "pokemon_name": {"type": "string", "description": "Name or Pokedex ID of the Pokemon", "required": True},
        },
        "sample_queries": [
            "Tell me about Pikachu",
            "Get stats for Charizard",
            "What type is Bulbasaur?",
        ],
    },

    # ── Space ──
    "get_space_photo": {
        "domain": "Space",
        "provider": "NASA APOD",
        "description": "Get NASA's Astronomy Picture of the Day.",
        "base_url": "https://api.nasa.gov/planetary/apod",
        "docs_url": "https://api.nasa.gov/",
        "openapi_url": None,
        "v1_schema": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format", "required": False},
        },
        "v2_schema": {
            "photo_date": {"type": "string", "description": "Date for the astronomy picture", "required": False},
        },
        "sample_queries": [
            "Show me today's NASA space photo",
            "NASA astronomy picture for 2024-01-01",
            "Space photo of the day",
        ],
    },

    # ── Geolocation ──
    "geolocate_ip": {
        "domain": "Geolocation",
        "provider": "ip-api",
        "description": "Get geolocation data for an IP address.",
        "base_url": "http://ip-api.com/json/{ip}",
        "docs_url": "http://ip-api.com/docs/api:json",
        "openapi_url": None,
        "v1_schema": {
            "ip": {"type": "string", "description": "IP address to look up", "required": True},
        },
        "v2_schema": {
            "ip_address": {"type": "string", "description": "IPv4 or IPv6 address to geolocate", "required": True},
        },
        "sample_queries": [
            "Where is IP 8.8.8.8 located?",
            "Geolocate IP 1.1.1.1",
            "What country is 203.0.113.0 from?",
        ],
    },

    # ── Sports ──
    "search_team": {
        "domain": "Sports",
        "provider": "TheSportsDB",
        "description": "Search for a sports team by name.",
        "base_url": "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
        "docs_url": "https://www.thesportsdb.com/api.php",
        "openapi_url": None,
        "v1_schema": {
            "team": {"type": "string", "description": "Team name to search for", "required": True},
        },
        "v2_schema": {
            "team_name": {"type": "string", "description": "Name of the sports team", "required": True},
        },
        "sample_queries": [
            "Search for Arsenal",
            "Find info about Barcelona FC",
            "Tell me about the Lakers",
        ],
    },

    # ── Music (ToolBench/RapidAPI) ──
    "search_song": {
        "domain": "Music",
        "provider": "Genius (ToolBench)",
        "description": "Search for songs, artists, and lyrics on Genius.",
        "base_url": "https://genius-song-lyrics1.p.rapidapi.com/search/",
        "docs_url": "https://rapidapi.com/Glavier/api/genius-song-lyrics1",
        "openapi_url": None,
        "rapidapi_host": "genius-song-lyrics1.p.rapidapi.com",
        "v1_schema": {
            "q": {"type": "string", "description": "Search query", "required": True},
            "per_page": {"type": "integer", "description": "Results per page", "required": False},
        },
        "v2_schema": {
            "search_query": {"type": "string", "description": "Text to search for", "required": True},
            "results_per_page": {"type": "integer", "description": "Number of results per page", "required": False},
        },
        "sample_queries": [
            "Search for Beatles songs",
            "Find lyrics for Bohemian Rhapsody",
            "Search Taylor Swift on Genius",
        ],
    },

    # ── Animals ──
    "get_dog_image": {
        "domain": "Animals",
        "provider": "Dog CEO",
        "description": "Get a random dog image, optionally by breed.",
        "base_url": "https://dog.ceo/api/breed/{breed}/images/random",
        "docs_url": "https://dog.ceo/dog-api/documentation/",
        "openapi_url": None,
        "v1_schema": {
            "breed": {"type": "string", "description": "Dog breed name, e.g. labrador", "required": True},
        },
        "v2_schema": {
            "dog_breed": {"type": "string", "description": "Breed of dog to get image for", "required": True},
        },
        "sample_queries": [
            "Show me a labrador picture",
            "Random husky image",
            "Get a poodle photo",
        ],
    },

    # ── Lifestyle ──
    "get_activity": {
        "domain": "Lifestyle",
        "provider": "Bored API",
        "description": "Get a random activity suggestion when bored.",
        "base_url": "https://bored-api.appbrewery.com/filter",
        "docs_url": "https://bored-api.appbrewery.com/",
        "openapi_url": None,
        "v1_schema": {
            "type": {"type": "string", "description": "Activity type: education, recreational, social, charity, cooking, relaxation, busywork", "required": True},
        },
        "v2_schema": {
            "activity_type": {"type": "string", "description": "Category of activity to suggest", "required": True},
        },
        "sample_queries": [
            "Suggest a recreational activity",
            "I'm bored, give me something educational to do",
            "Suggest a social activity",
        ],
    },

    # ── Data/Demographics ──
    "predict_age": {
        "domain": "Data",
        "provider": "Agify",
        "description": "Predict the age of a person based on their name.",
        "base_url": "https://api.agify.io",
        "docs_url": "https://agify.io/documentation",
        "openapi_url": None,
        "v1_schema": {
            "name": {"type": "string", "description": "First name to predict age for", "required": True},
        },
        "v2_schema": {
            "first_name": {"type": "string", "description": "Person's first name", "required": True},
        },
        "sample_queries": [
            "Predict age for the name Michael",
            "How old is someone named Priya likely to be?",
            "Age prediction for the name Sarah",
        ],
    },
}


def get_domains_summary():
    """Return domain → {icon, provider, tools, suggestions} for UI."""
    icons = {
        "Weather": "🌤️", "Geography": "🌍", "Crypto": "₿", "Finance": "💱",
        "Books": "📚", "Entertainment": "😂", "Education": "🎓", "Food & Drink": "🍹",
        "Trivia": "❓", "Gaming": "🎮", "Space": "🚀", "Geolocation": "📍",
        "Sports": "⚽", "Music": "🎵", "Animals": "🐕", "Lifestyle": "🎯", "Data": "📊",
    }
    domains = {}
    for tool_name, info in API_REGISTRY.items():
        d = info["domain"]
        if d not in domains:
            domains[d] = {"icon": icons.get(d, "🔧"), "tools": [], "providers": [], "suggestions": []}
        domains[d]["tools"].append(tool_name)
        if info["provider"] not in domains[d]["providers"]:
            domains[d]["providers"].append(info["provider"])
        domains[d]["suggestions"].extend(info["sample_queries"])
    return domains
