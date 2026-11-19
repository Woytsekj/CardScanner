DO $seed$
DECLARE
    v_tsp_set BIGINT;
    v_soi_set BIGINT;
    v_fixture_set BIGINT;
    v_fury_card BIGINT;
    v_avacyn_card BIGINT;
    v_fury_printing BIGINT;
    v_avacyn_printing BIGINT;
    v_falcon_printing BIGINT;
    v_avacyn_illustration_front BIGINT;
    v_avacyn_illustration_back BIGINT;
    v_fury_illustration BIGINT;
    v_falcon_illustration BIGINT;
    v_art_tag BIGINT;
    v_oracle_tag BIGINT;
    v_oracle_id TEXT := 'c8a7c3d8-4f17-4d1d-9b20-7c3e9d8f1001';
BEGIN
    -- Stable sets used by inventory, manual-add, and catalog-search tests.
    INSERT INTO catalog.sets (game_id, name, set_code, set_type)
    VALUES ('mtg', 'Time Spiral (CI fixture)', 'tsp', 'expansion')
    ON CONFLICT DO NOTHING;
    SELECT set_id INTO v_tsp_set
    FROM catalog.sets
    WHERE game_id = 'mtg' AND lower(set_code) = 'tsp'
    ORDER BY set_id LIMIT 1;

    INSERT INTO catalog.sets (game_id, name, set_code, set_type)
    VALUES ('mtg', 'Shadows over Innistrad (CI fixture)', 'soi', 'expansion')
    ON CONFLICT DO NOTHING;
    SELECT set_id INTO v_soi_set
    FROM catalog.sets
    WHERE game_id = 'mtg' AND lower(set_code) = 'soi'
    ORDER BY set_id LIMIT 1;

    INSERT INTO catalog.sets (game_id, name, set_code, set_type)
    VALUES ('mtg', 'CardScanner CI Fixture Set', 'fxt', 'expansion')
    ON CONFLICT DO NOTHING;
    SELECT set_id INTO v_fixture_set
    FROM catalog.sets
    WHERE game_id = 'mtg' AND lower(set_code) = 'fxt'
    ORDER BY set_id LIMIT 1;

    -- Canonical cards needed by inventory-count, inventory-move, and manual-add.
    SELECT card_id INTO v_fury_card
    FROM catalog.cards
    WHERE game_id = 'mtg' AND normalized_name = 'fury sliver'
    ORDER BY card_id LIMIT 1;
    IF v_fury_card IS NULL THEN
        INSERT INTO catalog.cards (game_id, name, type_line)
        VALUES ('mtg', 'Fury Sliver', 'Creature — Sliver')
        RETURNING card_id INTO v_fury_card;
    END IF;

    SELECT card_id INTO v_avacyn_card
    FROM catalog.cards
    WHERE game_id = 'mtg'
      AND normalized_name = 'archangel avacyn // avacyn, the purifier'
    ORDER BY card_id LIMIT 1;
    IF v_avacyn_card IS NULL THEN
        INSERT INTO catalog.cards (game_id, name, type_line)
        VALUES (
            'mtg',
            'Archangel Avacyn // Avacyn, the Purifier',
            'Legendary Creature — Angel'
        )
        RETURNING card_id INTO v_avacyn_card;
    END IF;

    -- Fury Sliver: known Scryfall printing + TCGCSV mapping + collector fallback.
    SELECT printing_id INTO v_fury_printing
    FROM catalog.external_ids
    WHERE game_id = 'mtg' AND provider_id = 'scryfall'
      AND entity_type = 'printing' AND external_id_type = 'scryfall_id'
      AND external_id = '0000579f-7b35-4ed3-b44c-db2a538066fe'
    LIMIT 1;
    IF v_fury_printing IS NULL THEN
        INSERT INTO catalog.printings (
            game_id, card_id, set_id, collector_number, rarity,
            available_finishes, metadata
        )
        VALUES (
            'mtg', v_fury_card, v_tsp_set, '157', 'uncommon',
            ARRAY['nonfoil','foil']::TEXT[], '{"name":"Fury Sliver"}'::JSONB
        )
        RETURNING printing_id INTO v_fury_printing;

        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type, external_id_type,
            external_id, printing_id
        )
        VALUES (
            'mtg', 'scryfall', 'printing', 'scryfall_id',
            '0000579f-7b35-4ed3-b44c-db2a538066fe', v_fury_printing
        )
        ON CONFLICT DO NOTHING;
    END IF;

    INSERT INTO catalog.printing_product_mappings (
        game_id, source_provider_id, source_external_printing_id,
        target_provider_id, printing_id, target_external_product_id,
        finishes, card_name, set_code, collector_number, resolved_at
    )
    VALUES (
        'mtg', 'scryfall', '0000579f-7b35-4ed3-b44c-db2a538066fe',
        'tcgcsv', v_fury_printing, '14240',
        '["normal","foil"]'::JSONB, 'Fury Sliver', 'tsp', '157', now()
    )
    ON CONFLICT (
        game_id, source_provider_id, source_external_printing_id,
        target_provider_id
    ) DO UPDATE SET
        printing_id = EXCLUDED.printing_id,
        target_external_product_id = EXCLUDED.target_external_product_id,
        finishes = EXCLUDED.finishes,
        card_name = EXCLUDED.card_name,
        set_code = EXCLUDED.set_code,
        collector_number = EXCLUDED.collector_number,
        resolved_at = EXCLUDED.resolved_at;

    -- Avacyn: two faces and the artwork/Oracle tag paths exercised by card-info.
    SELECT printing_id INTO v_avacyn_printing
    FROM catalog.external_ids
    WHERE game_id = 'mtg' AND provider_id = 'scryfall'
      AND entity_type = 'printing' AND external_id_type = 'scryfall_id'
      AND external_id = 'fe675f03-cbb5-4177-b7d7-64a30260ee2a'
    LIMIT 1;
    IF v_avacyn_printing IS NULL THEN
        INSERT INTO catalog.printings (
            game_id, card_id, set_id, collector_number, rarity,
            available_finishes, metadata
        )
        VALUES (
            'mtg', v_avacyn_card, v_soi_set, '5', 'mythic',
            ARRAY['nonfoil','foil']::TEXT[],
            '{"name":"Archangel Avacyn // Avacyn, the Purifier"}'::JSONB
        )
        RETURNING printing_id INTO v_avacyn_printing;

        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type, external_id_type,
            external_id, printing_id
        )
        VALUES (
            'mtg', 'scryfall', 'printing', 'scryfall_id',
            'fe675f03-cbb5-4177-b7d7-64a30260ee2a', v_avacyn_printing
        )
        ON CONFLICT DO NOTHING;
    END IF;

    INSERT INTO catalog.external_ids (
        game_id, provider_id, entity_type, external_id_type,
        external_id, card_id
    )
    VALUES (
        'mtg', 'scryfall', 'card', 'oracle_id', v_oracle_id, v_avacyn_card
    )
    ON CONFLICT DO NOTHING;

    UPDATE catalog.cards
    SET metadata = COALESCE(metadata, '{}'::JSONB)
                  || jsonb_build_object('oracle_id', v_oracle_id)
    WHERE game_id = 'mtg' AND card_id = v_avacyn_card;

    -- A second synthetic printing gives the fuzzy deck-comparison test two
    -- Scryfall printings with the same canonical Oracle identity.
    SELECT printing_id INTO v_avacyn_printing
    FROM catalog.external_ids
    WHERE game_id = 'mtg' AND provider_id = 'scryfall'
      AND entity_type = 'printing' AND external_id_type = 'scryfall_id'
      AND external_id = 'c3a8156a-09d9-4410-8891-bac1d4abc911'
    LIMIT 1;
    IF v_avacyn_printing IS NULL THEN
        INSERT INTO catalog.printings (
            game_id, card_id, set_id, collector_number, rarity,
            available_finishes, metadata
        )
        VALUES (
            'mtg', v_avacyn_card, v_soi_set, '5b', 'mythic',
            ARRAY['nonfoil','foil']::TEXT[],
            '{"name":"Archangel Avacyn // Avacyn, the Purifier"}'::JSONB
        )
        RETURNING printing_id INTO v_avacyn_printing;

        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type, external_id_type,
            external_id, printing_id
        )
        VALUES (
            'mtg', 'scryfall', 'printing', 'scryfall_id',
            'c3a8156a-09d9-4410-8891-bac1d4abc911', v_avacyn_printing
        )
        ON CONFLICT DO NOTHING;
    END IF;

    SELECT illustration_id INTO v_avacyn_illustration_front
    FROM catalog.illustrations
    WHERE game_id = 'mtg'
      AND metadata->>'ci_fixture' = 'avacyn-front'
    ORDER BY illustration_id LIMIT 1;
    IF v_avacyn_illustration_front IS NULL THEN
        INSERT INTO catalog.illustrations (game_id, metadata)
        VALUES ('mtg', '{"ci_fixture":"avacyn-front"}'::JSONB)
        RETURNING illustration_id INTO v_avacyn_illustration_front;
    END IF;

    SELECT illustration_id INTO v_avacyn_illustration_back
    FROM catalog.illustrations
    WHERE game_id = 'mtg'
      AND metadata->>'ci_fixture' = 'avacyn-back'
    ORDER BY illustration_id LIMIT 1;
    IF v_avacyn_illustration_back IS NULL THEN
        INSERT INTO catalog.illustrations (game_id, metadata)
        VALUES ('mtg', '{"ci_fixture":"avacyn-back"}'::JSONB)
        RETURNING illustration_id INTO v_avacyn_illustration_back;
    END IF;

    INSERT INTO catalog.printing_faces (
        printing_id, game_id, face_index, name, type_line, illustration_id
    )
    VALUES
        (
            v_avacyn_printing, 'mtg', 0, 'Archangel Avacyn',
            'Legendary Creature — Angel', v_avacyn_illustration_front
        ),
        (
            v_avacyn_printing, 'mtg', 1, 'Avacyn, the Purifier',
            'Legendary Creature — Angel', v_avacyn_illustration_back
        )
    ON CONFLICT (printing_id, face_index) DO UPDATE SET
        name = EXCLUDED.name,
        type_line = EXCLUDED.type_line,
        illustration_id = EXCLUDED.illustration_id;

    INSERT INTO catalog.provider_printing_metadata (
        game_id, provider_id, external_printing_id, printing_id,
        name, type_line, colors, colors_text, color_identity,
        mana_value, mana_cost, oracle_text, external_oracle_id,
        legalities, updated_at
    )
    VALUES (
        'mtg', 'scryfall', 'fe675f03-cbb5-4177-b7d7-64a30260ee2a',
        v_avacyn_printing,
        'Archangel Avacyn // Avacyn, the Purifier',
        'Legendary Creature — Angel',
        '["W"]'::JSONB, 'W', '["W"]'::JSONB,
        5, '{3}{W}{W}', 'Flying, vigilance', v_oracle_id,
        '{"standard":"not_legal","modern":"legal"}'::JSONB, now()
    )
    ON CONFLICT (game_id, provider_id, external_printing_id)
    DO UPDATE SET
        printing_id = EXCLUDED.printing_id,
        name = EXCLUDED.name,
        type_line = EXCLUDED.type_line,
        colors = EXCLUDED.colors,
        colors_text = EXCLUDED.colors_text,
        color_identity = EXCLUDED.color_identity,
        mana_value = EXCLUDED.mana_value,
        mana_cost = EXCLUDED.mana_cost,
        oracle_text = EXCLUDED.oracle_text,
        external_oracle_id = EXCLUDED.external_oracle_id,
        legalities = EXCLUDED.legalities,
        updated_at = EXCLUDED.updated_at;

    INSERT INTO catalog.tags (
        game_id, provider_id, external_tag_id, tag_type,
        slug, label, description
    )
    VALUES (
        'mtg', 'scryfall', 'ci-card-info-art-tag', 'art',
        'ci-card-info-art-tag', 'CI Artwork Fixture', 'CI card-info artwork tag'
    )
    ON CONFLICT (game_id, provider_id, external_tag_id) DO NOTHING;
    SELECT tag_id INTO v_art_tag
    FROM catalog.tags
    WHERE game_id='mtg' AND provider_id='scryfall'
      AND external_tag_id='ci-card-info-art-tag';

    INSERT INTO catalog.tags (
        game_id, provider_id, external_tag_id, tag_type,
        slug, label, description
    )
    VALUES (
        'mtg', 'scryfall', 'ci-card-info-oracle-tag', 'oracle',
        'ci-card-info-oracle-tag', 'CI Oracle Fixture', 'CI card-info Oracle tag'
    )
    ON CONFLICT (game_id, provider_id, external_tag_id) DO NOTHING;
    SELECT tag_id INTO v_oracle_tag
    FROM catalog.tags
    WHERE game_id='mtg' AND provider_id='scryfall'
      AND external_tag_id='ci-card-info-oracle-tag';

    INSERT INTO catalog.art_tag_assignments (
        game_id, provider_id, tag_id, illustration_id
    )
    VALUES
        ('mtg','scryfall',v_art_tag,v_avacyn_illustration_front),
        ('mtg','scryfall',v_art_tag,v_avacyn_illustration_back)
    ON CONFLICT DO NOTHING;

    INSERT INTO catalog.oracle_tag_assignments (
        game_id, provider_id, tag_id, external_oracle_id, card_id
    )
    VALUES (
        'mtg','scryfall',v_oracle_tag,v_oracle_id,v_avacyn_card
    )
    ON CONFLICT DO NOTHING;

    -- A-Falcon Abomination deliberately has no normalized cards row. Its
    -- name must be served from the printing metadata JSON.
    SELECT printing_id INTO v_falcon_printing
    FROM catalog.external_ids
    WHERE game_id = 'mtg' AND provider_id = 'scryfall'
      AND entity_type = 'printing' AND external_id_type = 'scryfall_id'
      AND external_id = '004c1078-0dea-4885-97b5-497f3f15c8c3'
    LIMIT 1;
    IF v_falcon_printing IS NULL THEN
        INSERT INTO catalog.printings (
            game_id, card_id, set_id, collector_number, rarity, metadata
        )
        VALUES (
            'mtg', NULL, v_fixture_set, '001', 'rare',
            '{"name":"A-Falcon Abomination"}'::JSONB
        )
        RETURNING printing_id INTO v_falcon_printing;

        INSERT INTO catalog.external_ids (
            game_id, provider_id, entity_type, external_id_type,
            external_id, printing_id
        )
        VALUES (
            'mtg', 'scryfall', 'printing', 'scryfall_id',
            '004c1078-0dea-4885-97b5-497f3f15c8c3', v_falcon_printing
        )
        ON CONFLICT DO NOTHING;
    END IF;

    SELECT illustration_id INTO v_falcon_illustration
    FROM catalog.illustrations
    WHERE game_id = 'mtg'
      AND metadata->>'ci_fixture' = 'a-falcon'
    ORDER BY illustration_id LIMIT 1;
    IF v_falcon_illustration IS NULL THEN
        INSERT INTO catalog.illustrations (game_id, metadata)
        VALUES ('mtg', '{"ci_fixture":"a-falcon"}'::JSONB)
        RETURNING illustration_id INTO v_falcon_illustration;
    END IF;

    INSERT INTO catalog.printing_faces (
        printing_id, game_id, face_index, name, type_line, illustration_id
    )
    VALUES (
        v_falcon_printing, 'mtg', 0, 'A-Falcon Abomination',
        'Creature — Test', v_falcon_illustration
    )
    ON CONFLICT (printing_id, face_index) DO UPDATE SET
        name = EXCLUDED.name,
        illustration_id = EXCLUDED.illustration_id;

    -- The bulk of tests exercise user/session behavior independently of any
    -- personal data. Seed only the historical superadmin identity they check.
    INSERT INTO app.users (
        username, password_hash, is_admin, role, is_superadmin, active
    )
    SELECT 'Stinkywar', 'ci-fixture-unused-password-hash', TRUE,
           'superadmin', TRUE, TRUE
    WHERE NOT EXISTS (
        SELECT 1 FROM app.users WHERE lower(btrim(username)) = 'stinkywar'
    );
END
$seed$;
