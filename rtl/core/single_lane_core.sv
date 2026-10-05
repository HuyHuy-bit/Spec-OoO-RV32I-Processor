`default_nettype none
module single_lane_core (
  input wire clk_i, rst_i,
  output wire instruction_request_valid_o,
  input wire instruction_request_ready_i,
  output memory_protocol_pkg::mem_request_t instruction_request_o,
  input wire instruction_response_valid_i,
  output wire instruction_response_ready_o,
  input memory_protocol_pkg::mem_response_t instruction_response_i,
  output wire data_request_valid_o,
  input wire data_request_ready_i,
  output memory_protocol_pkg::mem_request_t data_request_o,
  input wire data_response_valid_i,
  output wire data_response_ready_o,
  input memory_protocol_pkg::mem_response_t data_response_i,
  input wire commit_ready_i,
  output commit_event_pkg::commit_packet_t commit_o,
  output wire platform_fatal_o
);
  import platform_pkg::*;
  import single_lane_pkg::*;
  import memory_protocol_pkg::*;
  import commit_event_pkg::*;
  typedef enum logic [3:0] {
    FETCH_START, FETCH_WAIT, RENAME, EXECUTE, LOAD_REQUEST, LOAD_RESPONSE,
    WRITEBACK, RETIRE, STORE_REQUEST, STORE_RESPONSE
  } state_e;
  state_e state_q;
  logic [31:0] pc_q, instruction_q, source1_q, source2_q;
  logic [63:0] order_q;
  logic fetch_fault_q, fatal_q;
  logic [4:0] rob_index_q;
  logic [3:0] data_id_q;
  rename_entry_t renamed_q, rename_entry, head_entry;
  decoded_t decoded;
  commit_event_t pending_q, execute_event, head_event;
  mem_request_t data_request_q, execute_request;
  wire fetch_start_ready, fetch_valid, fetch_fault, fetch_fatal;
  wire fetch_request_valid, fetch_response_ready;
  wire [31:0] fetched_instruction;
  wire [5:0] source1_tag, source2_tag;
  wire source_ready, rename_ready, rob_ready, complete_ready, head_valid;
  wire [4:0] rob_index;
  wire [127:0] prf_data;
  wire [31:0] result, next_pc, address, cause, trap_value, csr_value, mtvec, mepc;
  wire execute_trap, csr_legal, unused_taken;
  commit_csr_effect_t [3:0] csr_effects;
  logic build_trap;
  logic [31:0] build_cause, build_trap_value, read_word, selected_word, load_value;
  logic [3:0] memory_mask;
  wire active = !rst_i && !platform_fatal_o;
  wire allocate = active && state_q == RENAME && rename_ready && rob_ready && source_ready;
  wire complete = active && state_q == WRITEBACK && complete_ready;
  wire accepted_write = complete && pending_q.rd_write_mask != 0;
  wire retire = active && state_q == RETIRE && head_valid && commit_ready_i;
  wire recover = retire && head_event.trap;
  wire data_wait = state_q == LOAD_RESPONSE || state_q == STORE_RESPONSE;
  assign platform_fatal_o = fatal_q || fetch_fatal;
  assign instruction_request_valid_o = fetch_request_valid && active;
  assign instruction_response_ready_o = fetch_response_ready && active;
  assign data_request_o = data_request_q;
  assign data_request_valid_o = active && (state_q == LOAD_REQUEST || state_q == STORE_REQUEST);
  assign data_response_ready_o = active && data_wait;

  fetch_single fetch (
    .clk_i, .rst_i, .start_i(active && state_q == FETCH_START), .pc_i(pc_q), .start_ready_o(fetch_start_ready),
    .request_valid_o(fetch_request_valid), .request_ready_i(instruction_request_ready_i && active),
    .request_o(instruction_request_o), .response_valid_i(instruction_response_valid_i),
    .response_ready_o(fetch_response_ready), .response_i(instruction_response_i),
    .valid_o(fetch_valid), .ready_i(active && state_q == FETCH_WAIT), .instruction_o(fetched_instruction),
    .fault_o(fetch_fault), .fatal_o(fetch_fatal)
  );
  decode_single decode (.instruction_i(instruction_q), .decoded_o(decoded));
  rename_single rename_stage (
    .clk_i, .rst_i, .recover_i(recover), .rs1_i(decoded.rs1), .rs2_i(decoded.rs2), .rd_i(decoded.rd),
    .source1_o(source1_tag), .source2_o(source2_tag), .sources_ready_o(source_ready),
    .allocate_i(allocate), .can_allocate_o(rename_ready), .entry_o(rename_entry),
    .wb_accept_i(accepted_write), .wb_destination_i(renamed_q.destination),
    .commit_i(retire && !head_event.trap), .commit_entry_i(head_entry)
  );
  prf_4r2w prf (
    .clk_i, .rst_i, .raddr_i({12'd0, source2_tag, source1_tag}), .rdata_o(prf_data),
    .wb_accept_i({1'b0, accepted_write}), .waddr_i({6'd0, renamed_q.destination}),
    .wdata_i({32'd0, pending_q.rd_value})
  );
  rob_single rob (
    .clk_i, .rst_i, .flush_i(1'b0), .allocate_i(allocate), .allocate_ready_o(rob_ready),
    .allocate_index_o(rob_index), .allocate_entry_i(rename_entry), .complete_i(complete),
    .complete_index_i(rob_index_q), .complete_event_i(pending_q), .complete_ready_o(complete_ready),
    .head_valid_o(head_valid), .retire_i(retire), .head_entry_o(head_entry), .head_event_o(head_event)
  );
  execute_single execute_stage (
    .decoded_i(decoded), .pc_i(pc_q), .instruction_i(instruction_q), .source1_i(source1_q), .source2_i(source2_q),
    .csr_value_i(csr_value), .mepc_i(mepc), .csr_legal_i(csr_legal), .result_o(result), .next_pc_o(next_pc),
    .address_o(address), .trap_o(execute_trap), .cause_o(cause), .trap_value_o(trap_value), .taken_o(unused_taken)
  );
  csr_single csr (
    .clk_i, .rst_i, .commit_i(retire), .commit_event_i(head_event), .decoded_i(decoded),
    .source_i(source1_q), .pc_i(pc_q), .trap_i(build_trap), .cause_i(build_cause), .trap_value_i(build_trap_value),
    .legal_o(csr_legal), .value_o(csr_value), .mtvec_o(mtvec), .mepc_o(mepc), .effects_o(csr_effects)
  );

  always_comb begin
    build_trap = execute_trap || fetch_fault_q;
    build_cause = fetch_fault_q ? CAUSE_INSTRUCTION_ACCESS_FAULT : cause;
    build_trap_value = fetch_fault_q ? pc_q : trap_value;
    if (state_q == LOAD_RESPONSE && data_response_valid_i && data_response_i.status == MEM_STATUS_ACCESS_FAULT) begin
      build_trap = 1;
      build_cause = CAUSE_LOAD_ACCESS_FAULT;
      build_trap_value = address;
    end
  end
  always_comb begin
    memory_mask = (decoded.funct3[1:0] == 0 ? 4'b0001 : decoded.funct3[1:0] == 1 ? 4'b0011 : 4'b1111) << address[1:0];
    execute_event = '0;
    execute_event.valid = 1;
    execute_event.order = order_q;
    execute_event.instruction = instruction_q;
    execute_event.privilege = 3;
    execute_event.pc_before = pc_q;
    execute_event.pc_after = build_trap ? mtvec : next_pc;
    execute_event.rs1_addr = decoded.rs1;
    execute_event.rs1_value = source1_q;
    execute_event.rs2_addr = decoded.rs2;
    execute_event.rs2_value = source2_q;
    execute_event.trap = build_trap;
    execute_event.retired = !build_trap;
    execute_event.csr_effects = csr_effects;
    if (build_trap) begin
      execute_event.trap_cause = build_cause;
      execute_event.trap_value = build_trap_value;
    end else begin
      if (decoded.rd != 0) begin
        execute_event.rd_addr = decoded.rd;
        execute_event.rd_value = result;
        execute_event.rd_write_mask = '1;
      end
      if (decoded.op inside {OP_LOAD, OP_STORE}) begin
        execute_event.mem_valid = 1;
        execute_event.mem_address = address;
        if (decoded.op == OP_STORE) begin
          execute_event.mem_write_mask = memory_mask;
          execute_event.mem_write_data = (source2_q << (address[1:0]*8)) & byte_mask(memory_mask);
        end else execute_event.mem_read_mask = memory_mask;
      end
    end
    execute_request = '0;
    execute_request.transaction_id = data_id_q;
    execute_request.write = decoded.op == OP_STORE;
    execute_request.uncached = !cached(address);
    if (execute_request.uncached) begin
      execute_request.uncached_size = mem_uncached_size_e'(decoded.funct3[1:0]);
      execute_request.address = address;
      execute_request.uncached_write_data = execute_event.mem_write_data;
      execute_request.uncached_write_strobe = execute_event.mem_write_mask;
    end else begin
      execute_request.address = {address[31:5], 5'd0};
      execute_request.line_write_mask = {28'd0, execute_event.mem_write_mask} << (address[4:2]*4);
      execute_request.line_write_data[address[4:2]*32 +: 32] = execute_event.mem_write_data;
    end
    read_word = data_request_q.uncached ? data_response_i.uncached_read_data : data_response_i.line_read_data[address[4:2]*32 +: 32];
    selected_word = read_word >> (address[1:0]*8);
    case (decoded.funct3)
      0: load_value = {{24{selected_word[7]}}, selected_word[7:0]};
      1: load_value = {{16{selected_word[15]}}, selected_word[15:0]};
      4: load_value = {24'd0, selected_word[7:0]};
      5: load_value = {16'd0, selected_word[15:0]};
      default: load_value = selected_word;
    endcase
    commit_o = '0;
    if (active && state_q == RETIRE && head_valid) commit_o.slots[0] = head_event;
  end

  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= FETCH_START;
      pc_q <= RESET_PC;
      instruction_q <= 0;
      source1_q <= 0;
      source2_q <= 0;
      order_q <= 0;
      fetch_fault_q <= 0;
      fatal_q <= 0;
      rob_index_q <= 0;
      data_id_q <= 0;
      renamed_q <= '0;
      pending_q <= '0;
      data_request_q <= '0;
    end else if (active) begin
      if (data_response_valid_i && (!data_wait || data_response_i.transaction_id != data_request_q.transaction_id
          || !(data_response_i.status inside {MEM_STATUS_OK, MEM_STATUS_ACCESS_FAULT})
          || ((data_request_q.write || data_response_i.status != MEM_STATUS_OK)
              && (data_response_i.line_read_data != 0 || data_response_i.uncached_read_data != 0))
          || (!data_request_q.uncached && data_response_i.uncached_read_data != 0)
          || (data_request_q.uncached && (data_response_i.line_read_data != 0
              || (data_response_i.uncached_read_data & ~byte_mask(memory_mask)) != 0)))) fatal_q <= 1;
      case (state_q)
        FETCH_START: if (fetch_start_ready) state_q <= FETCH_WAIT;
        FETCH_WAIT: if (fetch_valid) begin
          instruction_q <= fetched_instruction;
          fetch_fault_q <= fetch_fault;
          state_q <= RENAME;
        end
        RENAME: if (allocate) begin
          renamed_q <= rename_entry;
          rob_index_q <= rob_index;
          source1_q <= prf_data[31:0];
          source2_q <= prf_data[63:32];
          state_q <= EXECUTE;
        end
        EXECUTE: begin
          pending_q <= execute_event;
          data_request_q <= execute_request;
          state_q <= decoded.op == OP_LOAD && !build_trap ? LOAD_REQUEST : WRITEBACK;
        end
        LOAD_REQUEST: if (data_request_ready_i) state_q <= LOAD_RESPONSE;
        LOAD_RESPONSE: if (data_response_valid_i) begin
          pending_q <= execute_event;
          if (data_response_i.status == MEM_STATUS_OK) begin
            if (decoded.rd != 0) pending_q.rd_value <= load_value;
            pending_q.mem_read_data <= read_word & byte_mask(memory_mask);
          end
          data_id_q <= data_id_q + 1'b1;
          state_q <= WRITEBACK;
        end
        WRITEBACK: if (complete) state_q <= RETIRE;
        RETIRE: if (retire) begin
          pc_q <= head_event.pc_after;
          order_q <= order_q + 1'b1;
          state_q <= head_event.mem_write_mask != 0 ? STORE_REQUEST : FETCH_START;
        end
        STORE_REQUEST: if (data_request_ready_i) state_q <= STORE_RESPONSE;
        STORE_RESPONSE: if (data_response_valid_i) begin
          if (data_response_i.status != MEM_STATUS_OK) fatal_q <= 1;
          data_id_q <= data_id_q + 1'b1;
          state_q <= FETCH_START;
        end
        default: fatal_q <= 1;
      endcase
    end
  end
endmodule
`default_nettype wire
