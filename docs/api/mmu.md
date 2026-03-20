# MMU (Context Window Memory)

The MMU manages LLM context window pressure. When the context exceeds a token budget, the MMU evicts less-important messages to a semantic memory store.

!!! warning "Experimental"
    MMU is marked **experimental** — its API may change between minor versions.

## MMU

::: castor.mmu.core.MMU

## SemanticMemoryDriver

Abstract base class for memory storage backends. Implement this to add a custom vector store.

::: castor.mmu.driver.SemanticMemoryDriver
