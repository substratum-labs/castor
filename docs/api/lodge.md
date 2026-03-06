# Lodge (Context Window Memory)

The Lodge manages LLM context window pressure. When the context exceeds a token budget, the Lodge evicts less-important messages to a semantic memory store.

!!! warning "Experimental"
    CastorLodge is marked **experimental** — its API may change between minor versions.

## CastorLodge

::: castor.lodge.core.CastorLodge

## SemanticMemoryDriver

Abstract base class for memory storage backends. Implement this to add a custom vector store.

::: castor.lodge.driver.SemanticMemoryDriver
