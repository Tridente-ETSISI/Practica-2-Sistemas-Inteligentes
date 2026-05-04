# -*- coding: utf-8 -*-

# This sample demonstrates handling intents from an Alexa skill using the Alexa Skills Kit SDK for Python.
# Please visit https://alexa.design/cookbook for additional examples on implementing slots, dialog management,
# session persistence, api calls, and more.
# This sample is built using the handler classes approach in skill builder.
import logging
import ask_sdk_core.utils as ask_utils

from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler
from ask_sdk_core.dispatch_components import AbstractExceptionHandler
from ask_sdk_core.handler_input import HandlerInput

from ask_sdk_model import Response

from alexa_scrapper import get_movie_data

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class LaunchRequestHandler(AbstractRequestHandler):
    """Handler for Skill Launch."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool

        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "Bienvenido a tu skill de confianza sobre películas. Pregúntame el dato que quieras sobre cualquier película."

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    """Handler for Help Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = (
        "Puedes preguntarme por la nota, director, duración, "
        "géneros, sinopsis o fecha de estreno de cualquier película.")

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    """Single handler for Cancel and Stop Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return (ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or
                ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input))

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        speak_output = "Hasta luego."

        return (
            handler_input.response_builder
                .speak(speak_output)
                .response
        )

class FallbackIntentHandler(AbstractRequestHandler):
    """Single handler for Fallback Intent."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response
        logger.info("In FallbackIntentHandler")
        speech = "Perdona, no he entendido bien tu pregunta. Por favor, prueba a preguntar de otro modo."
        reprompt = "I didn't catch that. What can I help you with?"

        return handler_input.response_builder.speak(speech).ask(reprompt).response

class SessionEndedRequestHandler(AbstractRequestHandler):
    """Handler for Session End."""
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        # type: (HandlerInput) -> Response

        # Any cleanup logic goes here.

        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    """Generic error handling to capture any syntax or routing errors. If you receive an error
    stating the request handler chain is not found, you have not implemented a handler for
    the intent being invoked or included it in the skill builder below.
    """
    def can_handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> bool
        return True

    def handle(self, handler_input, exception):
        # type: (HandlerInput, Exception) -> Response
        logger.error(exception, exc_info=True)

        speak_output = "Perdona, no he entendido bien tu pregunta. Por favor, prueba a preguntar de otro modo."

        return (
            handler_input.response_builder
                .speak(speak_output)
                .ask(speak_output)
                .response
        )


class NotaIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar la nota de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("NotaIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"La nota de {data['title']} es de {data['vote_average']} sobre 10."

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


class DirectorIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar el director de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("DirectorIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"El director de {data['title']} es {data['director']}."

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


class GenerosIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar los géneros de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("GenerosIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"Los géneros de {data['title']} son {data['genres']}."

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


class FechaIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar la fecha de salida de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("FechaIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"La fecha de salida de {data['title']} fue el {data['release_date']}."

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


class SinopsisIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar la sinopsis de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("SinopsisIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"La sinopsis de {data['title']} es la siguiente: {data['overview']}"

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


class DuracionIntentHandler(AbstractRequestHandler):
    """
    Intent para recuperar la duración de cualquier película.
    """
    def can_handle(self, handler_input):
        # type: (HandlerInput) -> bool
        return ask_utils.is_intent_name("DuracionIntent")(handler_input)

    def handle(self, handler_input):
        movie_name = handler_input.request_envelope.request.intent.slots["pelicula"].value
        data = get_movie_data(movie_name)

        speech = f"La duración de {data['title']} es de {data['runtime']}."

        return (
            handler_input.response_builder
                .speak(speech)
                .ask("Pregúntame por otra película si quieres.")
                .response)


# The SkillBuilder object acts as the entry point for your skill, routing all request and response
# payloads to the handlers above. Make sure any new handlers or interceptors you've
# defined are included below. The order matters - they're processed top to bottom.


sb = SkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(HelpIntentHandler())

sb.add_request_handler(NotaIntentHandler())
sb.add_request_handler(DirectorIntentHandler())
sb.add_request_handler(GenerosIntentHandler())
sb.add_request_handler(FechaIntentHandler())
sb.add_request_handler(SinopsisIntentHandler())
sb.add_request_handler(DuracionIntentHandler())

sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())

sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()