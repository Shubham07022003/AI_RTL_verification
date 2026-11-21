// =============================================================================
// Illustrative UVM testbench skeleton for the counter DUT.
//
// NOTE: This is a skeleton demonstrating the UVM structure the agent detects
// and analyzes. It does NOT compile with Icarus Verilog or Verilator - UVM
// requires a UVM-capable simulator (VCS, Xcelium, Questa, riviera, ...).
// =============================================================================

import uvm_pkg::*;

// -----------------------------------------------------------------------------
// DUT instantiation (kept in the env)
// -----------------------------------------------------------------------------
module tb_counter; // module header; in real UVM the top module instantiates uvm_*

    // A UVM testbench instantiates the top test class, not the DUT directly.
    uvm_test_top top;

    initial begin
        $display("[info] tb_counter: starting UVM testbench");
    end

endmodule

// -----------------------------------------------------------------------------
// Sequence
// -----------------------------------------------------------------------------
class counter_sequence extends uvm_sequence #(counter_item);
    `uvm_object_utils(counter_sequence)

    task body();
        counter_item item;
        item = counter_item::type_id::create("item");
        item.rst = 1;
        item.rst = 0;
        item.load = 1;
        item.data_in = 8'hAA;
        item.send_to_sequencer(this);
    endtask
endclass

// -----------------------------------------------------------------------------
// Component class skeletons (full implementations require a working UVM env)
// -----------------------------------------------------------------------------
class counter_driver extends uvm_driver;
    `uvm_component_utils(counter_driver)

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction
endclass

class counter_monitor extends uvm_monitor;
    `uvm_component_utils(counter_monitor)

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction
endclass

class counter_scoreboard extends uvm_scoreboard;
    `uvm_component_utils(counter_scoreboard)

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction
endclass

class counter_sequencer extends uvm_sequencer;
    `uvm_component_utils(counter_sequencer)

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction
endclass

// -----------------------------------------------------------------------------
// Env / components
// -----------------------------------------------------------------------------
class counter_env extends uvm_env;
    `uvm_component_utils(counter_env)

    counter_driver driver;
    counter_monitor monitor;
    counter_scoreboard scoreboard;
    counter_sequencer sequencer;

    virtual counter_if vif;

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
        super.build_phase(phase);
        driver     = counter_driver::type_id::create("driver", this);
        monitor    = counter_monitor::type_id::create("monitor", this);
        scoreboard = counter_scoreboard::type_id::create("scoreboard", this);
        sequencer  = counter_sequencer::type_id::create("sequencer", this);
        vif        = counter_if::type_id::create("vif", this);
        // The DUT is instantiated inside build_phase:
        counter dut (.clk(vif.clk), .rst(vif.rst), .load(vif.load),
                    .data_in(vif.data_in), .count(vif.count));
    endfunction
endclass

// -----------------------------------------------------------------------------
// Test
// -----------------------------------------------------------------------------
class counter_test extends uvm_test;
    `uvm_component_utils(counter_test)

    counter_env env;
    counter_sequence sequence;

    function new(string name, uvm_component parent);
        super.new(name, parent);
    endfunction

    virtual function void build_phase(uvm_phase phase);
        super.build_phase(phase);
        env = counter_env::type_id::create("env", this);
        sequence = counter_sequence::type_id::create("sequence", this);
        uvm_config_db #(virtual counter_if)::set(this, "env.vif", "vif", env.vif);
    endfunction

    virtual task run_phase(uvm_phase phase);
        phase.raise_objection(this);
        sequence.start(env.sequencer);
        phase.drop_objection(this);
        super.run_phase(phase);
    endtask
endclass
