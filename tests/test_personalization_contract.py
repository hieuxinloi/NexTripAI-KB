from nextrip_graphrag.versions.v6.schemas import ConversationContext


def test_personalization_context_round_trips_with_conversation_state() -> None:
    context = ConversationContext(
        turn_count=2,
        cities=["Quy Nhơn"],
        personalization={
            "profile_revision": 3,
            "preferred_concepts": ["beach"],
            "excluded_concepts": ["nightclub"],
        },
    )
    payload = context.model_dump(mode="json")
    restored = ConversationContext.model_validate(payload)

    assert restored.personalization["profile_revision"] == 3
    assert restored.personalization["preferred_concepts"] == ["beach"]
