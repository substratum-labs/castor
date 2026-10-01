#!/usr/bin/env node
import { readLocalModelPin } from './ollama_model_adapter.mjs';
const pin = await readLocalModelPin();
process.stdout.write(`${JSON.stringify(pin)}\n`);
