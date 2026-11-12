"""Current visibility of persisted natural knowledge answers."""

from app.errors import AppError


def answer_visible(conn, auth, cards):
    from app.knowledge_retrieval import read_evidence_in

    for card in cards or []:
        if card.get("kind") != "knowledge_quote" or not card.get("reference_label"):
            continue
        try:
            read_evidence_in(conn, auth, str(card["citation_id"]))
        except (AppError, KeyError, ValueError, TypeError):
            return False
    return True


def project_turn(conn, auth, turn):
    if answer_visible(conn, auth, turn.get("final_cards")):
        return turn
    return {
        **turn,
        "final_message": "这条回答引用的知识资料已更新或不可用，请重新查询；其他业务卡片仍可核对。",
        "final_cards": [
            card for card in turn["final_cards"] if card.get("kind") != "knowledge_quote"
        ],
    }
