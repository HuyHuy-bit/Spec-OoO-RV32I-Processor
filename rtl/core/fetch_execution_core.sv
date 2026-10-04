`default_nettype none
module fetch_execution_core #(parameter bit FRONTEND_FAULTS = 1, parameter bit TRAP_SERVICE = 1, parameter bit MEMORY_SERVICE = 0) (
  input wire clk_i, rst_i, enable_i, flush_i, drained_i,
  input wire memory_issue_allowed_i, admit_valid_i,
  input wire [12:0] admit_id_i,
  output wire admission_valid_o,
  output wire [12:0] admission_id_o,
  output wire [31:0] admission_address_o, admission_data_o,
  output wire [3:0] admission_mask_o,
  output wire [1:0] admission_size_o,
  output wire memory_prepare_o, memory_busy_o, memory_committed_o, memory_irrevocable_o, memory_fatal_o,
  output wire flush_ready_o,
  output wire data_request_valid_o,
  input wire data_request_ready_i,
  output memory_protocol_pkg::mem_request_t data_request_o,
  input wire data_response_valid_i,
  output wire data_response_ready_o,
  input memory_protocol_pkg::mem_response_t data_response_i,
  input wire [31:0] flush_pc_i,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i,
  input wire [1:0] execution_ready_i, completion_enable_i, retire_ready_i,
  input wire resolve_grant_i,
  output wire [1:0] retire_valid_o, retire_accept_o,
  output commit_event_pkg::commit_event_t [1:0] retire_event_o,
  output wire redirect_o,
  output wire [31:0] redirect_pc_o,
  output wire [1:0] fetch_valid_o, dispatch_o, unsupported_o, producer_busy_o,
  output wire [63:0] fetch_pc_o, fetch_instruction_o,
  output wire fetch_busy_o, fetch_fault_o,
  output wire [31:0] fetch_fault_cause_o,
  input wire trap_ready_i,
  output wire trap_valid_o, trap_accept_o,
  output commit_event_pkg::commit_event_t trap_event_o,
  output wire backend_fault_o,
  output commit_event_pkg::commit_event_t backend_fault_event_o,
  output wire [5:0] occupancy_o,
  output wire system_prepare_o, system_busy_o,
  output wire identity_drain_o, fatal_o
);
  wire fetch_fatal;
  assign fatal_o = fetch_fatal || memory_fatal_o;
  assign flush_ready_o = !memory_irrevocable_o;
  wire [1:0] supported, backend_valid, frontend_fault;
  wire [63:0] frontend_cause, frontend_value;
  if (FRONTEND_FAULTS) begin : faults
    frontend_fault_decode decode (
      .valid_i(fetch_valid_o), .instruction_i(fetch_instruction_o), .pc_i(fetch_pc_o),
      .fetch_fault_i(fetch_fault_o), .fetch_cause_i(fetch_fault_cause_o),
      .fault_o(frontend_fault), .cause_o(frontend_cause), .value_o(frontend_value)
    );
  end else begin : no_faults
    assign frontend_fault = 0;
    assign frontend_cause = 0;
    assign frontend_value = 0;
  end
  wire [63:0] predicted_pc = {fetch_pc_o[63:32] + 32'd4, fetch_pc_o[31:0] + 32'd4};
  wire branch_redirect, system_redirect;
  wire [31:0] system_pc;
  wire [31:0] branch_pc;
  wire running = !rst_i && !fatal_o;
  // Drain identity holders, flush to the committed next PC, then recycle identities.
  typedef enum logic [1:0] {RUN, STOP, RESTART, DRAIN} recycle_t;
  recycle_t recycle_q;
  logic [31:0] committed_pc_q;
  wire quiet = occupancy_o == 0 && flush_ready_o && !memory_busy_o && !data_request_valid_o
               && producer_busy_o == 0 && !system_busy_o && !fetch_busy_o;
  wire flush = flush_i || recycle_q == RESTART;
  wire drained = drained_i || recycle_q == DRAIN;
  wire fetch_enable = enable_i && recycle_q == RUN;
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      recycle_q <= RUN;
      committed_pc_q <= platform_pkg::RESET_PC;
    end else begin
      case (recycle_q)
        RUN: if (identity_drain_o && !fatal_o) recycle_q <= STOP;
        STOP: if (!identity_drain_o) recycle_q <= RUN;
              else if (quiet) recycle_q <= RESTART;
        RESTART: recycle_q <= DRAIN;
        DRAIN: recycle_q <= RUN;
      endcase
      if (trap_accept_o) committed_pc_q <= trap_event_o.pc_after;
      else if (retire_accept_o[1]) committed_pc_q <= retire_event_o[1].pc_after;
      else if (retire_accept_o[0]) committed_pc_q <= retire_event_o[0].pc_after;
      else if (flush_i && running) committed_pc_q <= flush_pc_i;
    end
  end
  assign redirect_o = running && (flush || system_redirect || branch_redirect);
  assign redirect_pc_o = flush_i ? flush_pc_i : recycle_q == RESTART ? committed_pc_q
                       : system_redirect ? system_pc : branch_pc;
  assign backend_valid = fetch_valid_o & {2{running && (FRONTEND_FAULTS || !fetch_fault_o) && !drained}};
  assign unsupported_o = fetch_valid_o & ~supported & {2{!fetch_fault_o}};

  fetch_two_wide frontend (
    .clk_i, .rst_i, .enable_i(fetch_enable && !drained && !memory_fatal_o),
    .redirect_i(redirect_o), .redirect_pc_i(redirect_pc_o),
    .request_valid_o, .request_ready_i, .request_o,
    .response_valid_i, .response_ready_o, .response_i,
    .valid_o(fetch_valid_o), .take_i(dispatch_o), .instruction_o(fetch_instruction_o), .pc_o(fetch_pc_o),
    .fault_o(fetch_fault_o), .fault_cause_o(fetch_fault_cause_o), .busy_o(fetch_busy_o), .fatal_o(fetch_fatal)
  );
  control_flow_backend #(.SYSTEM_SERVICE(TRAP_SERVICE), .MEMORY_SERVICE(MEMORY_SERVICE)) backend (
    .memory_issue_allowed_i(memory_issue_allowed_i && running), .admit_valid_i, .admit_id_i,
    .admission_valid_o, .admission_id_o, .admission_address_o, .admission_data_o, .admission_mask_o, .admission_size_o,
    .memory_prepare_o, .memory_busy_o, .memory_committed_o, .memory_irrevocable_o, .memory_fatal_o,
    .data_request_valid_o, .data_request_ready_i, .data_request_o,
    .data_response_valid_i, .data_response_ready_o, .data_response_i,
    .cancel_system_i(flush || !running), .system_prepare_o, .system_busy_o,
    .system_redirect_o(system_redirect), .system_redirect_pc_o(system_pc),
    .system_trap_valid_o(trap_valid_o), .system_trap_event_o(trap_event_o),
    .clk_i, .rst_i, .flush_i(flush && running), .drained_i(drained),
    .valid_i(backend_valid), .instruction_i(fetch_instruction_o), .pc_i(fetch_pc_o),
    .frontend_fault_i(frontend_fault), .frontend_cause_i(frontend_cause), .frontend_value_i(frontend_value),
    .predicted_pc_i(predicted_pc), .predicted_taken_i(2'b00),
    .supported_o(supported), .allocate_accept_o(dispatch_o), .allocate_id_o(),
    .execution_ready_i(execution_ready_i & {2{running}}),
    .completion_enable_i(completion_enable_i & {2{running}}),
    .retire_ready_i(retire_ready_i & {2{running}}), .resolve_grant_i(resolve_grant_i && running),
    .resolve_valid_o(), .resolve_accept_o(), .resolve_taken_o(), .resolve_mispredict_o(),
    .resolve_id_o(), .resolve_pc_o(branch_pc), .redirect_o(branch_redirect),
    .issue_o(), .producer_busy_o, .issue_id_o(),
    .completion_valid_o(), .completion_accept_o(), .wb_accept_o(), .completion_id_o(), .completion_event_o(),
    .retire_valid_o, .retire_accept_o, .retire_event_o,
    .trap_ready_i(trap_ready_i && TRAP_SERVICE && running && !flush), .trap_accept_o,
    .fault_pending_o(backend_fault_o), .fault_event_o(backend_fault_event_o),
    .head_id_o(), .occupancy_o, .issue_occupancy_o(), .checkpoint_valid_o(), .identity_drain_o
  );
`ifndef SYNTHESIS
  always_ff @(posedge clk_i) begin
    if (!rst_i) begin
      assert (!flush || flush_ready_o) else $fatal(1, "FETCH_CORE_MMIO_FLUSH");
      assert (!drained || !memory_busy_o) else $fatal(1, "FETCH_CORE_MEMORY_DRAIN");
      assert (trap_accept_o == (trap_valid_o && trap_ready_i))
        else $fatal(1, "FETCH_CORE_TRAP_ATOMIC");
      assert (!trap_accept_o || (redirect_o && !branch_redirect && retire_accept_o == 0))
        else $fatal(1, "FETCH_CORE_TRAP_RECOVERY");
      assert (!drained || (!fetch_enable && !fetch_busy_o && fetch_valid_o == 0 && !fatal_o))
        else $fatal(1, "FETCH_CORE_DRAIN");
      assert (!redirect_o || (dispatch_o == 0 && (retire_accept_o == 0
              || (!flush && system_redirect && !trap_accept_o && retire_accept_o == 1))))
        else $fatal(1, "FETCH_CORE_REDIRECT");
    end
  end
`endif
endmodule
`default_nettype wire
