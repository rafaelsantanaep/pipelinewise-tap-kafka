import orjson
from confluent_kafka.serialization import Deserializer
from confluent_kafka.serialization import SerializationError


class JSONSimpleDeserializer(Deserializer):
    """
    Deserializes a Python object from JSON formatted bytes.
    """
    def __call__(self, value, ctx):
        """
        Deserializes a Python object from JSON formatted bytes
        Args:
            value (bytes): bytes to be deserialized
            ctx (SerializationContext): Metadata pertaining to the serialization
                operation
        Raises:
            SerializerError if an error occurs during deserialization.
        Returns:
            Python object if data is not None, otherwise None
        """
        if value is None:
            return None

        try:
            return orjson.loads(value)
        except orjson.JSONDecodeError as e:
            raise SerializationError(str(e))