from dotenv import load_dotenv
from openai import OpenAI
from mem0 import Memory
import os

load_dotenv()

config = {
    "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "vector_store": {
        "provider": "supabase",
        "config": {
            "connection_string": "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
            "collection_name": "memories",
        },
    },
}

openai_client = OpenAI()

print(f"database_url::  {os.environ['DATABASE_URL']}")

print("here 111")
memory = Memory.from_config(config)
print("here 222")


def chat_with_memories(message: str, user_id: str = "default_user") -> str:
    # Retrieve relevant memories
    relevant_memories = memory.search(query=message, user_id=user_id, limit=3)
    memories_str = "\n".join(
        f"- {entry['memory']}" for entry in relevant_memories["results"]
    )

    # Generate Assistant response
    system_prompt = f"You are a helpful AI. Answer the question based on query and memories.\nUser Memories:\n{memories_str}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]
    response = openai_client.chat.completions.create(
        model=os.getenv("MODEL_CHOICE", "gpt-4o-mini"), messages=messages
    )
    assistant_response = response.choices[0].message.content

    # Create new memories from the conversation
    messages.append({"role": "assistant", "content": assistant_response})
    memory.add(messages, user_id=user_id)

    return assistant_response


def main():
    print("Chat with AI (type 'exit' to quit)")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() == "exit":
            print("Goodbye!")
            break
        print(f"AI: {chat_with_memories(user_input)}")


if __name__ == "__main__":
    main()
