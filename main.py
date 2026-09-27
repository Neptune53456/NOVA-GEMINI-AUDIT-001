
from command_router import handle_direct_command
from conversation_manager import ConversationManager
from edit_controller import EditController
from memory import init_db
from smart_memory import init_smart_memory
from system_action_controller import SystemActionController


def main():
    """Initialise les services puis exécute la boucle interactive."""
    init_db()
    init_smart_memory()

    print("IA prête.")
    print("Tape 'quit' pour quitter.\n")

    edit_controller = EditController()
    system_action_controller = SystemActionController()
    conversation_manager = ConversationManager(
        system_action_controller=system_action_controller,
    )

    while True:
        user_input = input("Toi : ").strip()

        if user_input.lower() == "quit":
            print("À bientôt.")
            break

        if not user_input:
            continue

        edit_result = edit_controller.handle_confirmation(user_input)
        if edit_result["handled"]:
            continue

        system_action_result = conversation_manager.handle_system_confirmation(user_input)
        if system_action_result["handled"]:
            print("\nIA :", system_action_result["response"], "\n")
            continue

        direct_result = handle_direct_command(user_input)
        if direct_result["handled"]:
            conversation_response = direct_result.get("conversation_response")
            if conversation_response is not None:
                conversation_manager.add_exchange(
                    user_input,
                    conversation_response,
                )
            continue

        edit_result = edit_controller.handle(user_input)
        if edit_result["handled"]:
            continue

        answer = conversation_manager.handle(user_input)
        if answer is not None:
            print("\nIA :", answer, "\n")


if __name__ == "__main__":
    main()
