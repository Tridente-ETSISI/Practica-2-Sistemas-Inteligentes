import requests
from datetime import datetime

API_KEY = "81fc975ed54d429d247ca977a74db148"


def fix_release_date(fecha_str):
    # Diccionario para traducir los meses
    months = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }

    # Convertir el texto a objeto de fecha
    date = datetime.strptime(fecha_str, "%Y-%m-%d")

    # Construir el formato deseado
    return f"{date.day} de {months[date.month]} de {date.year}"


def fix_runtime(runtime):
    hours = runtime // 60
    minutes = runtime % 60
    return f"{hours} horas y {minutes} minutos"


def fix_genres(genres):
    genres = [g.lower() for g in genres]

    if len(genres) == 1:
        speech_genres = genres[0]
    elif len(genres) == 2:
        speech_genres = f"{genres[0]} y {genres[1]}"
    elif len(genres) >= 3:
        speech_genres = f"{', '.join(genres[:-1])} y {genres[-1]}"
    return speech_genres


movies_cache = {}

def get_movie_data(movie_name):
    if movie_name in movies_cache:
        return movies_cache[movie_name]
    else:
        data = movie_scrape(movie_name)
        movies_cache[movie_name] = data
        return data


def movie_scrape(movie_name):
    # Obtiene el ID de la película
    url = "https://api.themoviedb.org/3/search/movie"
    params = {
        "api_key": API_KEY,
        "query": movie_name,
        "language": "es-ES"}

    response = requests.get(url, params=params)
    movie_id = response.json()['results'][0]['id']

    # Obtiene los datos de la película
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {
        "api_key": API_KEY,
        "language": "es-ES"}

    response = requests.get(url, params=params)
    movie_data = response.json()

    # Obtiene el director de la película
    credits_url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits"
    params = {
        "api_key": API_KEY}

    response = requests.get(credits_url, params=params)
    credits = response.json()
    for person in credits["crew"]:
        if person["job"] == "Director":
            director = person["name"]
            break

    data = {
        "title": movie_data['title'],
        "release_date": fix_release_date(movie_data['release_date']),
        "overview": movie_data['overview'],
        "genres": fix_genres([genre['name'] for genre in movie_data['genres']]),
        "runtime": fix_runtime(movie_data['runtime']),
        "vote_average": round(float(movie_data['vote_average']), 1),
        "director": director
    }

    return data