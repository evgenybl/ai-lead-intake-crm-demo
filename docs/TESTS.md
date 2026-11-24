# The tests

Generated from `tests/` by `python -I tools/list_tests.py`; CI fails if it is out of date. Run every test with
`python -I run_tests.py`. A test's name says what it proves; the line under a name is its docstring.

233 test methods in 16 files. Where a test uses subTest, one method checks several cases.

## tests/test_audit_and_store.py

Integration tests on a real desk file: the append-only trail (section 3.6) and the store's rules.

### AuditTrailTests

- `test_the_database_refuses_to_change_an_audit_row`
- `test_the_database_refuses_to_remove_an_audit_row`
- `test_the_database_refuses_a_row_that_does_not_follow_the_last`
- `test_insert_or_replace_cannot_overwrite_a_row`
- `test_the_chain_adds_up_and_the_triggers_are_there`
- `test_with_the_triggers_gone_a_changed_row_breaks_the_chain`
- `test_with_the_triggers_gone_a_removed_row_breaks_the_chain`
- `test_each_row_names_time_key_event_detail_and_actor`

### StoreRuleTests

- `test_one_live_approval_per_enquiry`
- `test_a_rejection_needs_a_reason_in_the_database_too`
- `test_a_state_outside_the_named_states_is_refused`
- `test_one_first_arrival_per_enquiry`
- `test_a_desk_is_never_created_over_another`

## tests/test_cli.py

End-to-end tests that drive `desk.py` through main(argv), on new files in a temporary folder.

### StoryTests

The whole story of the invented data, command by command.

- `test_the_whole_story`
- `test_outputs_are_the_same_for_the_same_fixed_clock`

### CommandTests

- `test_a_line_that_is_not_json_is_named_and_the_others_are_taken`
- `test_a_missing_desk_or_file_is_a_usage_error`
- `test_a_bad_fixed_time_is_a_usage_error`
- `test_approve_checks_the_plan_the_person_saw`
- `test_a_decision_needs_a_person_not_the_system`
- `test_reject_needs_a_reason_and_then_nothing_is_written`
- `test_withdraw_puts_the_plan_back_to_wait`
- `test_a_two_contacts_hold_is_resolved_as_either_contact_never_as_a_second_holder_of_the_email`
- `test_a_possible_duplicate_resolved_as_new_creates_a_contact`
- `test_a_possible_duplicate_resolved_as_the_contact_adds_to_it`
- `test_status_names_each_adapter`
- `test_the_trail_shows_one_enquirys_life_in_order`

## tests/test_controls.py

The five controls of section 6. Each breaks one property in memory or in its own temporary files, and asserts that `desk.py doctor`, or the matching test's check, reports the break. A control that passed while the property was broken would mean the doctor could not see that failure.

### ControlTests

- `test_control_1_the_key_lookup_made_to_miss_a_duplicate_note_appears`
- `test_control_2_the_unique_index_dropped_a_second_contact_appears`
- `test_control_3_the_gates_approval_check_removed_a_write_with_no_approval_is_found`
- `test_control_4_the_allow_list_widened_a_foreign_operation_is_found`
- `test_control_5_the_audit_triggers_dropped_a_changed_row_is_found`

## tests/test_data.py

The invented data of section 4: the files equal the generator's output, and each case does what it is for.

### GeneratorTests

- `test_the_files_equal_the_generators_output`
- `test_the_generator_gives_the_same_output_every_time`
- `test_the_data_holds_every_case_section_4_asks_for`
- `test_everyone_is_invented_and_reachable_only_at_placeholders`

### CasesReachTheirStatesTests

- `test_each_case_reaches_the_state_it_is_for`
- `test_the_hidden_instruction_still_waits_for_a_person`

## tests/test_docs_and_demo.py

The documents say only what a run printed; the demo and the examples are what their commands make.

### DocumentTests

- `test_every_image_a_markdown_file_embeds_exists`
- `test_every_block_quoted_from_the_examples_is_a_real_slice_of_its_file`
- `test_every_quoted_model_sentence_is_a_real_slice_of_its_recorded_answer`
- `test_the_status_output_on_the_readme_is_what_the_command_prints`
- `test_the_test_list_is_generated_from_the_tests`
- `test_the_licence_is_mit_with_its_copyright_line`
- `test_ci_pins_its_actions_by_commit_and_runs_both_systems_and_pythons`
- `test_the_readme_never_says_live_for_a_path_that_was_run`

### PictureToolTests

- `test_it_draws_exactly_the_lines_it_is_given`
- `test_the_closing_table_fits_and_its_passing_lines_hold_no_red_word`

### DemoTests

- `test_the_demo_has_every_case_of_section_5`
- `test_one_case_runs_on_its_own_new_files`
- `test_the_examples_and_the_picture_are_what_a_new_run_makes`

## tests/test_foundations.py

Unit tests: the clock, canonical JSON, normalisation, the draft stand-in, the allow-list, the state table.

### ClockTests

- `test_a_fixed_clock_returns_its_time`
- `test_at_comes_before_the_environment`
- `test_without_either_the_system_clock_is_used`
- `test_a_malformed_or_impossible_time_is_refused`

### CanonicalTests

- `test_sorted_keys_fixed_separators_utf8`
- `test_the_same_value_gives_the_same_sha_whatever_the_key_order`
- `test_nan_and_lone_surrogates_are_refused`

### NormaliseTests

- `test_email_is_trimmed_and_lowercased`
- `test_phone_is_its_ascii_digits`
- `test_name_ignores_case_spacing_and_punctuation_not_letters`
- `test_message_whitespace_is_collapsed`

### DraftTests

- `test_the_stand_in_says_it_is_not_a_model`
- `test_the_summary_is_at_most_forty_words`
- `test_a_decimal_point_does_not_end_the_sentence`

### AllowListTests

- `test_exactly_four_operations`
- `test_outside_and_repeated`

### StateTableTests

- `test_every_target_is_a_named_state`
- `test_no_move_out_of_a_final_state`
- `test_an_unlisted_move_is_refused`
- `test_every_state_can_be_reached`

## tests/test_gate.py

Integration tests: the one write gate (section 3.5) on real desk and practice CRM files.

### GateTests

- `test_an_approved_plan_is_applied_and_the_approval_is_used`
- `test_refused_with_no_approval_nothing_written`
- `test_refused_twice_after_a_successful_apply`
- `test_an_operation_outside_the_allow_list_is_refused_before_anything_leaves`
- `test_an_approval_for_another_plan_is_refused_and_the_enquiry_waits_again`
- `test_an_approval_whose_sha_differs_from_the_stored_plan_is_refused`
- `test_a_stored_plan_changed_after_approval_is_refused`
- `test_a_dry_run_writes_nothing_even_when_approved`
- `test_the_gate_refuses_any_write_in_a_dry_run_even_if_called_directly`
- `test_crm_down_then_retry_completes_and_doubles_nothing`
- `test_a_lost_answer_is_found_again_not_written_again`
- `test_a_lost_ledger_writes_nothing_twice`
- `test_a_lost_ledger_mid_plan_on_a_returning_contact_completes_once`
- `test_a_retry_does_not_plan_again_once_a_step_has_started`
- `test_withdrawn_after_a_failure_then_approved_again_completes_once`
- `test_a_retry_after_a_failure_that_left_nothing_started_plans_again`

## tests/test_http_crm.py

The HTTP CRM adapter against the strict fake (section 3.7, section 6). Mock: no real CRM is reached.

### StrictFakeTests

- `test_the_fake_refuses_an_unknown_field_path_or_query`
- `test_the_fake_refuses_a_wrong_token`

### AdapterTests

- `test_every_call_of_the_api_round_trips`
- `test_a_second_contact_with_one_email_is_a_conflict`
- `test_an_error_names_the_call_and_never_the_token`
- `test_a_hang_up_is_an_error_not_an_answer`
- `test_a_server_that_is_not_there_is_an_error`

### DeskOverHttpTests

The whole flow with the HTTP adapter in place of the practice CRM.

- `test_a_new_contact_through_the_adapter`
- `test_a_returning_contact_is_filled_never_doubled`
- `test_a_500_then_a_retry_completes_and_doubles_nothing`
- `test_a_hang_up_after_the_write_with_search_lagging_is_found_on_the_contact`
- `test_a_note_on_another_contact_is_found_by_search_confirmed_by_its_first_line`
- `test_the_crm_down_at_arrival_leaves_the_enquiry_received_and_counted`

## tests/test_model_step.py

Section 3.4: the request and its guard, the strict answer, the three backends, and the hidden instruction.

### RequestTests

- `test_the_request_never_carries_the_name_the_email_or_the_phone`: For every invented enquiry that reaches the model.
- `test_the_message_is_scrubbed_of_the_senders_own_values_in_any_case`
- `test_the_marker_word_invented_is_not_a_name_word`
- `test_any_email_or_phone_shape_is_scrubbed_but_dates_and_figures_are_kept`
- `test_the_guard_refuses_a_request_that_still_holds_personal_data`
- `test_the_request_is_named_by_its_sha256`
- `test_the_request_files_are_what_the_generator_writes`
- `test_the_constructed_answers_are_what_the_tool_writes_and_say_so`

### DraftLineTests

How the dry run shows the model step: never "None" where an answer was refused.

- `test_each_outcome_in_one_line`

### AnswerTests

- `test_a_good_answer_passes`
- `test_an_unknown_key_refuses_the_whole_answer`
- `test_the_shape_and_the_limits`
- `test_the_reply_holds_no_email_phone_or_web_address`
- `test_every_number_in_digits_must_appear_in_the_message`
- `test_neither_text_holds_the_senders_own_values`
- `test_parsing_is_strict`

### RecordedBackendTests

The label "Recorded" is earned only by a matching recording in recorded/answers/.

- `test_no_recording_means_no_ai_text`
- `test_a_matching_recording_is_labelled_recorded`
- `test_a_constructed_answer_is_never_labelled_recorded`
- `test_a_changed_answer_file_or_request_is_not_used`
- `test_an_answer_that_fails_is_not_used_and_says_why`
- `test_the_template_and_the_backend_choice`

### AnthropicBackendTests

The live path, against a local mock only.

- `test_the_label_names_a_custom_endpoint`
- `test_one_request_carries_only_the_request_text`
- `test_a_failing_answer_gets_one_retry_with_the_reasons`
- `test_two_failing_answers_are_not_used`
- `test_a_refusal_or_an_error_leaves_no_ai_text`
- `test_a_request_holding_personal_data_is_never_sent`

### HiddenInstructionTests

Section 3.4's three proofs for the enquiry whose message tells the software to approve itself.

- `test_whatever_a_model_answers_the_enquiry_still_needs_a_persons_approval`
- `test_an_answer_with_any_key_beyond_the_four_is_refused_whole`
- `test_the_recorded_real_answer_for_it_is_in_the_repository_as_it_came`

### ReplayDifferenceTests

- `test_a_replay_with_different_content_stays_a_replay_and_is_shown_for_a_person`
- `test_a_name_that_a_spreadsheet_would_read_as_a_formula_is_refused`

## tests/test_pages.py

Section 3.8: the desk pages, `render` and `serve`. No script on any page; everything from an enquiry or a model is escaped; Approve and Reject take two locks and work only with --allow-decisions.

### RenderTests

- `test_render_writes_the_desk_and_a_page_per_enquiry_with_links_that_exist`
- `test_no_page_holds_a_script_or_a_form`
- `test_every_page_is_self_contained_and_loads_nothing_from_a_network`
- `test_markup_in_a_message_is_shown_as_text`
- `test_the_counts_come_first_in_plain_words`
- `test_each_enquiry_names_the_label_of_its_model_step`: Every invented enquiry that reaches the model has a recorded answer; four of them were refused whole.
- `test_where_no_recording_exists_a_constructed_answer_is_labelled_so`
- `test_a_replay_with_different_content_is_shown_on_the_desk`

### EscapingTests

- `test_everything_from_an_enquiry_or_a_model_is_escaped`

### ServeWithoutDecisionsTests

- `test_the_desk_and_an_enquiry_page_are_served_with_no_script_allowed`
- `test_without_the_switch_the_page_shows_the_commands_and_takes_no_decision`
- `test_a_request_addressed_to_another_name_is_not_answered`
- `test_with_no_secret_the_intake_answers_503_and_the_desk_still_shows`

### ServeWithDecisionsTests

- `test_the_forms_appear_with_two_locks_and_a_token`
- `test_approve_needs_the_ticked_box_and_a_name`
- `test_a_form_without_this_desks_token_or_from_elsewhere_is_refused`
- `test_an_approval_names_the_plan_the_person_saw`
- `test_reject_needs_a_reason`
- `test_the_intake_endpoint_still_works_beside_the_desk`

## tests/test_planner.py

Unit and integration tests: the plan (section 3.5), its sha256, and when it changes.

### PlannerTests

- `test_a_new_contact_plan`
- `test_the_note_and_the_task_begin_with_the_key`
- `test_the_sha_is_the_sha_of_the_canonical_plan`
- `test_returning_fills_empty_fields_and_shows_a_difference_without_writing_it`
- `test_a_difference_of_case_or_spacing_is_not_shown`
- `test_an_email_another_contact_holds_is_shown_not_filled`
- `test_the_sha_changes_when_the_contact_now_exists`
- `test_the_sha_changes_when_an_empty_field_was_filled_meanwhile`
- `test_the_sha_does_not_change_for_what_does_not_matter`
- `test_only_allow_listed_operations_each_at_most_once`
- `test_a_hold_has_no_plan`
- `test_a_resolution_as_new_stops_applying_once_the_email_is_taken`
- `test_a_resolution_to_a_missing_contact_stops_applying`
- `test_the_plan_holds_no_draft_text`

## tests/test_practice_crm.py

Integration tests: the practice CRM on a real SQLite file (section 3.7).

### PracticeCRMTests

- `test_the_unique_index_refuses_a_second_contact_for_one_email`
- `test_contacts_without_an_email_do_not_collide`
- `test_fill_writes_only_empty_fields_and_says_which`
- `test_fill_refuses_an_email_another_contact_holds`
- `test_fill_refuses_a_field_that_is_not_a_contact_field`
- `test_search_reads_underscores_as_text_not_as_wildcards`
- `test_origin_key_finds_the_contact_an_enquiry_created`
- `test_notes_and_tasks_need_an_existing_contact`
- `test_a_missing_file_is_an_error_not_a_new_crm`

## tests/test_promises.py

The four promises of section 2, one test each, named by the promise; and the doctor catching a break of each.

### PromiseTests

- `test_promise_1_no_lead_is_lost_every_arrival_ends_in_one_named_state_and_they_add_up`
- `test_promise_2_no_duplicate_is_created_a_replay_writes_nothing_and_a_returning_contact_is_not_doubled`
- `test_promise_3_nothing_is_written_before_a_person_approves`
- `test_promise_4_every_step_is_on_the_record_in_a_trail_the_database_refuses_to_change`

### DoctorFindsEachBrokenPromiseTests

Beyond the five controls: the doctor names a break of each promise, made directly in the files.

- `test_promise_1_a_lost_lead_is_found`
- `test_promise_1_an_enquiry_with_no_arrival_is_found`
- `test_promise_1_a_failure_with_no_error_named_is_found`
- `test_promise_2_a_second_note_with_one_key_is_found`
- `test_promise_3_a_write_that_bypassed_the_gate_is_found`
- `test_promise_4_a_state_changed_off_the_record_is_found`
- `test_the_doctor_command_fails_when_a_check_fails`

## tests/test_rules.py

Rules of the repository itself (section 1), checked on every file, and the one-gate rule on the code.

### RepositoryRuleTests

- `test_there_are_files_to_check`
- `test_no_absolute_path_in_any_file`
- `test_no_key_shaped_or_token_shaped_literal_in_any_file`: A sha256 digest (exactly 64 lowercase hex digits) names a file's content; it is not a secret.
- `test_every_email_address_is_at_example_com`
- `test_every_text_file_has_lf_line_ends`
- `test_every_python_file_is_valid_python_3_11`
- `test_servers_bind_127_0_0_1_only`

### OneGateTests

- `test_only_the_gate_calls_a_crm_write`: Inside intake/, a call of create_contact, fill_empty_fields, add_note or add_task appears in intake/gate.py and nowhere else (the adapters in intake/crm/ define them).
- `test_the_gate_reaches_each_write_through_its_one_write_method`

## tests/test_server.py

End-to-end tests of POST /intake (section 3.1): it fails closed, and it binds 127.0.0.1 only.

### NoSecretTests

- `test_with_no_secret_configured_every_request_is_refused_with_503`
- `test_a_short_secret_counts_as_none`

### IntakeTests

- `test_it_binds_127_0_0_1_only`
- `test_a_wrong_or_missing_secret_is_401_and_nothing_is_kept`
- `test_a_body_that_is_not_one_json_object_is_400`
- `test_a_new_enquiry_is_201_with_its_key_and_state`
- `test_a_replay_is_200_and_points_to_the_original`
- `test_a_refused_enquiry_is_kept_and_answered_by_its_state_only`
- `test_other_paths_methods_and_sizes`
- `test_no_content_length_is_411`
- `test_the_secret_is_never_written_to_the_log`
- `test_markup_in_a_message_is_kept_as_text`

### ListenCommandTests

`python -I desk.py listen` as a real process, on a port the system assigns.

- `test_the_listen_command_takes_an_enquiry`

## tests/test_validate_and_identity.py

Unit tests: validation (section 3.2), the idempotency key and contact matching (section 3.3).

### ValidateTests

- `test_a_good_enquiry_is_taken`
- `test_each_refusal_of_section_3_2_with_its_reason`
- `test_the_boundaries`
- `test_consent_must_be_the_json_value_true`
- `test_every_reason_is_kept_not_only_the_first`
- `test_a_phone_with_too_few_digits_is_not_a_phone`
- `test_a_field_of_the_wrong_type_is_a_reason_not_a_crash`
- `test_received_at`
- `test_times_are_read_the_same_way_on_every_python`
- `test_unknown_fields_are_ignored_and_named`

### KeyTests

- `test_a_valid_source_and_source_id_make_the_key`
- `test_an_invalid_source_id_falls_back_to_the_hash`
- `test_the_source_hash_cannot_imitate_a_hashed_key`
- `test_the_hash_reads_normalised_values_and_the_date_only`
- `test_the_date_is_the_utc_date`

### MatchTests

The table of section 3.3, against a real practice CRM file.

- `test_new`
- `test_returning_by_email_whatever_its_case`
- `test_returning_by_phone_as_digits`
- `test_returning_when_email_and_phone_point_to_the_same_contact`
- `test_held_when_the_email_and_the_phone_belong_to_two_contacts`
- `test_held_when_the_phone_alone_belongs_to_two_contacts`
- `test_possible_duplicate_needs_the_same_name_and_company`
- `test_never_a_similar_name`
- `test_an_empty_company_never_makes_a_possible_duplicate`
- `test_an_exact_key_wins_over_a_same_name`
